"""异常页识别（原型已验证的判据，别重写）。

判定顺序很重要：先看 HTTP 与体积，再看登录墙/风控，最后看强结构标记。
**结构标记命中 < 2 必须判异常** —— 否则空壳页会被当成有效快照，下游静默失效。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["AbnormalResult", "sniff_abnormal"]

_LOGIN_WALL = re.compile(r"亲，请登录|请登录后|扫码登录|login\.taobao|验证码|滑动验证|安全验证", re.I)
_RISK_PAGE = re.compile(r"访问受限|系统繁忙|稍后再试|调用出错|被挤爆|休息一下", re.I)
_EMPTY_BODY = re.compile(r"^\s*(?:<!doctype[^>]*>\s*)?(?:<html[^>]*>\s*)?<body[^>]*>\s*</body>", re.I)

MIN_STRONG_MARKERS = 2
MIN_BODY_BYTES = 1024


@dataclass(frozen=True)
class AbnormalResult:
    abnormal_type: str
    reason: str

    @property
    def is_abnormal(self) -> bool:
        return bool(self.abnormal_type)


OK = AbnormalResult("", "")


def sniff_abnormal(
    *, http_status: int, body_bytes: int, text: str, strong_hits: int
) -> AbnormalResult:
    """返回异常类型；正常返回 ``AbnormalResult('', '')``。

    异常类型对应运维看板的分组维度（`abnormal_type`）：
    ``HTTP_ERROR | RATE_LIMITED | LOGIN_WALL | RISK_CONTROL | EMPTY_BODY | STRUCTURE_CHANGED``
    """
    if http_status == 429:
        return AbnormalResult("RATE_LIMITED", "HTTP 429，已被限流")
    if http_status >= 500:
        return AbnormalResult("HTTP_ERROR", f"HTTP {http_status}")
    if http_status in (403, 401):
        return AbnormalResult("RISK_CONTROL", f"HTTP {http_status}，疑似风控")
    if http_status >= 400:
        return AbnormalResult("HTTP_ERROR", f"HTTP {http_status}")
    if body_bytes < MIN_BODY_BYTES:
        return AbnormalResult("EMPTY_BODY", f"响应体过小（{body_bytes} B）")
    if not (text or "").strip() or _EMPTY_BODY.search(text[:2000]):
        return AbnormalResult("EMPTY_BODY", "响应体为空壳")
    if _LOGIN_WALL.search(text):
        return AbnormalResult("LOGIN_WALL", "页面要求登录/验证码")
    if _RISK_PAGE.search(text) and strong_hits < MIN_STRONG_MARKERS:
        return AbnormalResult("RISK_CONTROL", "命中风控提示页")
    if strong_hits < MIN_STRONG_MARKERS:
        return AbnormalResult("STRUCTURE_CHANGED", f"强结构标记仅命中 {strong_hits} 个（<{MIN_STRONG_MARKERS}）")
    return OK
