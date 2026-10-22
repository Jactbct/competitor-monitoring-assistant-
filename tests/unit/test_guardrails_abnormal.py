"""合规护栏与异常页识别 —— 这两块是「能不能对外证明合规」的依据，必须有测试。"""

from __future__ import annotations

import pytest

from domain.collect.abnormal import sniff_abnormal
from domain.collect.guardrails import (
    FORBIDDEN_HEADERS,
    GuardrailError,
    RobotsPolicy,
    build_headers,
    normalize_url,
    url_hash,
    validate_url,
)

UA = "TaobaoCompetitorMonitor/0.1 (+mailto:ops@example.com)"


def test_only_https_allowed():
    with pytest.raises(GuardrailError):
        validate_url("http://item.taobao.com/item.htm?id=1")


def test_host_must_be_whitelisted():
    with pytest.raises(GuardrailError):
        validate_url("https://example.com/item?id=1")


def test_login_and_api_paths_blocked():
    with pytest.raises(GuardrailError):
        validate_url("https://item.taobao.com/login")
    with pytest.raises(GuardrailError):
        validate_url("https://item.taobao.com/api/item?id=1")


def test_headers_have_no_login_state():
    headers = build_headers(UA)
    assert headers["User-Agent"] == UA
    assert not (FORBIDDEN_HEADERS & {k.lower() for k in headers})
    assert "Cookie" not in headers


def test_normalize_url_drops_tracking_params():
    url = normalize_url("https://item.taobao.com/item.htm?id=1001&spm=a1z10.1&utm=x")
    assert url == "https://item.taobao.com/item.htm?id=1001"
    assert url_hash(url) == url_hash("https://item.taobao.com/item.htm?id=1001")


def test_robots_disallow_respected():
    policy = RobotsPolicy.parse("User-agent: *\nDisallow: /item.htm\n")
    assert policy.permitted("/shop/1") is True
    assert policy.permitted("/item.htm?id=1") is False


def test_robots_other_agent_ignored():
    policy = RobotsPolicy.parse("User-agent: Googlebot\nDisallow: /\n")
    assert policy.permitted("/item.htm") is True


@pytest.mark.parametrize(
    ("status", "size", "text", "hits", "expected"),
    [
        (429, 20000, "<html>正常页</html>", 6, "RATE_LIMITED"),
        (503, 20000, "<html>系统繁忙</html>", 0, "HTTP_ERROR"),
        (403, 20000, "<html>访问受限</html>", 0, "RISK_CONTROL"),
        (200, 300, "<html>tiny</html>", 6, "EMPTY_BODY"),
        (200, 20000, "<html>亲，请登录后再查看</html>", 0, "LOGIN_WALL"),
        (200, 20000, "<html><body><div class='ph'></div></body></html>", 1, "STRUCTURE_CHANGED"),
        (200, 20000, "<html>tb-rmb-num tb-main-title J_ImgBooth</html>", 3, ""),
    ],
)
def test_abnormal_classification(status, size, text, hits, expected):
    result = sniff_abnormal(http_status=status, body_bytes=size, text=text, strong_hits=hits)
    assert result.abnormal_type == expected


def test_structure_changed_is_caught_before_downstream():
    """强结构标记不足必须判异常，否则空壳页会被当成有效快照，下游静默失效。"""
    result = sniff_abnormal(http_status=200, body_bytes=5000, text="<html>改版了</html>", strong_hits=0)
    assert result.abnormal_type == "STRUCTURE_CHANGED"
    assert result.is_abnormal
