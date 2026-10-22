"""采集客户端：两种模式（fixture / http），出口护栏全部走 :mod:`.guardrails`。

``fixture`` 模式读本地样本页，用于演示与联调（不产生任何外部请求）；
``http`` 模式是真实只读抓取，**必须合规评审通过才能开**。

关于条件请求：带上 ``If-None-Match`` / ``If-Modified-Since``，站点未改动时回 304。
这是同类工具（changedetection.io、Scrapy 生态）一致的第一条优化 —— 一个 304
几乎不花站点资源，也不花我们的带宽预算，比任何「降频技巧」都更礼貌。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from core.config import get_settings
from domain.collect.guardrails import (
    GuardrailError,
    RobotsPolicy,
    build_headers,
    normalize_url,
    validate_url,
)

__all__ = ["FetchResult", "fetch_page", "list_fixtures", "pick_fixture"]


@dataclass
class FetchResult:
    status_code: int
    body: bytes
    final_url: str
    elapsed_ms: int
    content_type: str = "text/html"
    source: str = "fixture"  # fixture|http
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    retry_after_s: int | None = None
    crawl_delay_s: int | None = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


_robots_cache: dict[str, RobotsPolicy] = {}


def list_fixtures(fixture_dir: Path | None = None) -> list[Path]:
    directory = fixture_dir or get_settings().collector.fixture_dir
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.html"))


def pick_fixture(url: str, fixture_dir: Path | None = None) -> Path | None:
    """按 item_id 命中样本页；命中不到就按 url 哈希稳定选一个（演示可重复）。"""
    fixtures = list_fixtures(fixture_dir)
    if not fixtures:
        return None
    item = ""
    for part in (urlparse(url).query or "").split("&"):
        key, _, value = part.partition("=")
        if key in {"id", "itemId", "item_id"} and value.isdigit():
            item = value
            break
    if item:
        for path in fixtures:
            if path.stem.split("_")[-1] == item or path.stem == item:
                return path
    digest = hashlib.sha256(normalize_url(url).encode()).digest()
    return fixtures[digest[0] % len(fixtures)]


def _fetch_fixture(url: str) -> FetchResult:
    started = time.perf_counter()
    path = pick_fixture(url)
    if path is None:
        raise GuardrailError(f"fixture 模式下找不到样本页（目录为空或未生成）：{url}")
    body = path.read_bytes()
    return FetchResult(
        status_code=200,
        body=body,
        final_url=url,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        source="fixture",
    )


def _parse_retry_after(raw: str | None) -> int | None:
    """Retry-After 可能是秒数，也可能是 HTTP 日期。统一取秒，上限 24 小时。"""
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return min(int(raw), 86400)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    seconds = int((when - datetime.now(UTC)).total_seconds())
    return max(0, min(seconds, 86400))


def _robots_policy(client: httpx.Client, url: str, user_agent: str) -> RobotsPolicy:
    parsed = urlparse(url)
    host = f"{parsed.scheme}://{parsed.netloc}"
    if host in _robots_cache:
        return _robots_cache[host]
    try:
        response = client.get(f"{host}/robots.txt", headers=build_headers(user_agent), timeout=5.0)
        policy = RobotsPolicy.parse(response.text if response.status_code == 200 else "")
    except httpx.HTTPError:
        policy = RobotsPolicy()
    _robots_cache[host] = policy
    return policy


def _fetch_http(url: str, etag: str | None, last_modified: str | None) -> FetchResult:
    settings = get_settings()
    validate_url(url)
    started = time.perf_counter()
    headers = build_headers(settings.collector.user_agent, etag=etag, last_modified=last_modified)

    with httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(settings.collector.timeout_s, connect=5.0),
        http2=False,
    ) as client:
        policy = _robots_policy(client, url, settings.collector.user_agent)
        if settings.collector.robots_enforce and not policy.permitted(urlparse(url).path or "/"):
            raise GuardrailError(f"robots.txt 禁止抓取：{urlparse(url).path}")

        response = client.get(url, headers=headers)
        elapsed = int((time.perf_counter() - started) * 1000)

        # 只有真正的重定向才拦（304 也落在 3xx 区间，不能当重定向处理）
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location", "")
            raise GuardrailError(f"发生重定向（不跟随，避免进入登录流）：{location[:80]}")

        if response.status_code == 304:
            return FetchResult(
                status_code=304,
                body=b"",
                final_url=str(response.url),
                elapsed_ms=elapsed,
                content_type=response.headers.get("content-type", "text/html"),
                source="http",
                not_modified=True,
                etag=etag,
                last_modified=last_modified,
                crawl_delay_s=policy.crawl_delay_s,
            )

        return FetchResult(
            status_code=response.status_code,
            body=response.content[: settings.collector.max_bytes],
            final_url=str(response.url),
            elapsed_ms=elapsed,
            content_type=response.headers.get("content-type", "text/html"),
            source="http",
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            retry_after_s=_parse_retry_after(response.headers.get("retry-after")),
            crawl_delay_s=policy.crawl_delay_s,
        )


def fetch_page(
    url: str, *, etag: str | None = None, last_modified: str | None = None
) -> FetchResult:
    settings = get_settings()
    normalized = normalize_url(url)
    if settings.collector.mode == "fixture":
        return _fetch_fixture(normalized)
    return _fetch_http(normalized, etag, last_modified)
