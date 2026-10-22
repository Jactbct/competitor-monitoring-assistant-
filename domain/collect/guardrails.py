"""采集合规护栏（代码级，可被审计 —— 对外证明「我们没有违规采集」的唯一依据）。

这些断言不能用「承诺」代替：
* 只允许 https GET；
* 请求头白名单，出现 Cookie/Authorization 直接抛错；
* 命中登录/后台/接口路径黑名单直接拒绝；
* robots 命中 Disallow 就跳过；
* 频率上限只在配置文件里，**不提供后台旋钮**（§8.3）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

__all__ = [
    "FORBIDDEN_HEADERS",
    "GuardrailError",
    "allowed_by_robots",
    "build_headers",
    "normalize_url",
    "url_hash",
    "validate_url",
]

#: 允许采集的域名（白名单，防止被当成通用爬虫）
#: 允许采集的域名。
#: 新增域名不是「顺手加一行」——它等于扩大抓取面，必须同时确认：robots 允许、公开页、
#: 不涉及登录态，并走一次合规评审。京东加进来只代表「允许」，真实抓取仍要
#: COLLECTOR_MODE=http 才会发生（默认 fixture 不产生任何外部请求）。
ALLOWED_HOSTS = (
    "taobao.com",
    "tmall.com",
    "detail.tmall.com",
    "item.taobao.com",
    "jd.com",
    "item.jd.com",
    "jd.hk",
)

#: 采集只允许带这几个头（小写比较，避免大小写绕过）
HEADER_WHITELIST = frozenset(
    {
        "user-agent",
        "accept",
        "accept-language",
        "accept-encoding",
        "referer",
        # 条件请求头：只表达「我已有哪个版本」，不携带任何登录态
        "if-none-match",
        "if-modified-since",
    }
)

#: 明确禁止携带的头（登录态）
FORBIDDEN_HEADERS = frozenset({"cookie", "authorization", "x-token", "x-csrf-token"})

#: 登录/后台/接口路径黑名单
_BLOCKED_PATH = re.compile(
    r"/(?:login|signin|register|cart|buy|order|member|mytaobao|seller|admin|api|graphql|message)(?:/|$|\?)",
    re.I,
)
_BLOCKED_SCHEME = {"http", "file", "ftp", "javascript"}


class GuardrailError(RuntimeError):
    """护栏拦截。**它不是业务异常**，出现在日志里就说明代码写错了。"""


def normalize_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if not parsed.netloc:
        raise GuardrailError(f"URL 不合法：{url!r}")
    host = parsed.hostname or ""
    path = parsed.path or "/"
    query = ""
    # 只保留 id 参数，去掉跟踪参数（避免同一商品被算成多个 target）
    match = re.search(r"(?:^|[?&])(?:id|itemId|item_id)=(\d+)", parsed.query)
    if match:
        query = f"?id={match.group(1)}"
    return f"https://{host}{path}{query}"


def url_hash(url: str) -> str:
    import hashlib

    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def platform_for(url: str) -> str:
    """按域名判定平台，用于 Target.platform（影响解析适配器与页面展示）。"""
    host = (urlparse(url).hostname or "").lower()
    if host == "jd.com" or host.endswith(".jd.com") or host.endswith(".jd.hk"):
        return "jd"
    return "taobao"


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme in _BLOCKED_SCHEME:
        raise GuardrailError(f"仅允许 https 公开页，实际为 {parsed.scheme}")
    if parsed.scheme != "https":
        raise GuardrailError(f"仅允许 https，实际为 {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise GuardrailError(f"域名不在白名单内：{host}")
    if _BLOCKED_PATH.search(parsed.path or "/"):
        raise GuardrailError(f"命中登录/后台/接口路径黑名单：{parsed.path}")
    return url


def build_headers(
    user_agent: str, *, etag: str | None = None, last_modified: str | None = None
) -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    lowered = {k.lower() for k in headers}
    if FORBIDDEN_HEADERS & lowered:
        raise GuardrailError("请求头中出现登录态字段")
    if not lowered <= HEADER_WHITELIST:
        raise GuardrailError("请求头超出白名单")
    return headers


@dataclass
class RobotsPolicy:
    """极简 robots.txt 解析：处理 User-agent: * 的 Disallow / Allow / Crawl-delay。"""

    disallow: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()
    crawl_delay_s: int | None = None

    @classmethod
    def parse(cls, text: str) -> RobotsPolicy:
        disallow: list[str] = []
        allow: list[str] = []
        crawl_delay_s: int | None = None
        applies = False
        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "user-agent":
                applies = value == "*"
            elif applies and key == "disallow" and value:
                disallow.append(value)
            elif applies and key == "allow" and value:
                allow.append(value)
            elif applies and key == "crawl-delay":
                try:
                    delay = int(float(value))
                except ValueError:
                    continue
                if delay > 0:
                    crawl_delay_s = delay
        return cls(disallow=tuple(disallow), allow=tuple(allow), crawl_delay_s=crawl_delay_s)

    def permitted(self, path: str) -> bool:
        path = path or "/"
        if any(path.startswith(pattern) for pattern in self.allow):
            return True
        return not any(path.startswith(pattern) for pattern in self.disallow)


def allowed_by_robots(robots_text: str, url: str) -> bool:
    parsed = urlparse(url)
    return RobotsPolicy.parse(robots_text).permitted(parsed.path or "/")
