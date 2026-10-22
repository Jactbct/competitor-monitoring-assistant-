"""条件请求（304）与 Retry-After —— 借鉴同类开源工具后新增的两条标准行为。

为什么要测：304 分支一旦写错，会把「页面没变」当成「采集失败」，
或者把空 body 当有效快照落库（比漏采危险得多）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from domain.collect.client import _parse_retry_after, fetch_page
from domain.collect.guardrails import RobotsPolicy, build_headers


class FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None, content: bytes = b"<html>ok</html>"):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self.is_redirect = 300 <= status_code < 400
        self.url = "https://item.taobao.com/item.htm?id=1"

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class FakeClient:
    """记录请求头，用来断言条件请求确实发出去了。"""

    last_headers: dict = {}
    responses: list[FakeResponse] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, timeout=None):
        if str(url).endswith("/robots.txt"):
            return FakeResponse(404, {}, b"")
        FakeClient.last_headers = dict(headers or {})
        return FakeClient.responses.pop(0)


@pytest.fixture
def http_mode(monkeypatch, settings_like):
    from domain.collect import client as client_mod

    monkeypatch.setattr(client_mod.httpx, "Client", FakeClient)
    monkeypatch.setattr(client_mod, "get_settings", lambda: settings_like)
    client_mod._robots_cache.clear()
    return client_mod


class _Cfg:
    class collector:  # noqa: N801
        mode = "http"
        user_agent = "TaobaoCompetitorMonitor/0.1 (+mailto:ops@example.com)"
        timeout_s = 5.0
        max_bytes = 3_000_000
        robots_enforce = True


@pytest.fixture
def settings_like():
    return _Cfg()


def test_conditional_headers_are_sent(http_mode):
    FakeClient.responses = [FakeResponse(200, {"etag": 'W/"abc"', "last-modified": "Wed, 21 Oct 2026 10:00:00 GMT"})]
    result = fetch_page(
        "https://item.taobao.com/item.htm?id=1", etag='W/"old"', last_modified="Tue, 20 Oct 2026 10:00:00 GMT"
    )

    assert FakeClient.last_headers.get("If-None-Match") == 'W/"old"'
    assert FakeClient.last_headers.get("If-Modified-Since") == "Tue, 20 Oct 2026 10:00:00 GMT"
    assert result.not_modified is False
    assert result.etag == 'W/"abc"'


def test_304_is_reported_as_not_modified(http_mode):
    FakeClient.responses = [FakeResponse(304, {})]
    result = fetch_page("https://item.taobao.com/item.htm?id=1", etag='W/"same"')

    assert result.status_code == 304
    assert result.not_modified is True
    assert result.body == b""


def test_retry_after_seconds_and_http_date():
    assert _parse_retry_after("120") == 120
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("not-a-date") is None
    # 日期形式：取一个远未来时间，应该被夹到 24 小时上限
    assert _parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") == 86400


def test_retry_after_captured_on_429(http_mode):
    FakeClient.responses = [FakeResponse(429, {"retry-after": "60"})]
    result = fetch_page("https://item.taobao.com/item.htm?id=1")
    assert result.status_code == 429
    assert result.retry_after_s == 60


def test_conditional_headers_pass_guardrail_whitelist():
    headers = build_headers(
        "TaobaoCompetitorMonitor/0.1 (+mailto:ops@example.com)", etag='W/"x"', last_modified="Wed, 21 Oct 2026 10:00:00 GMT"
    )
    assert headers["If-None-Match"] == 'W/"x"'
    assert "cookie" not in {k.lower() for k in headers}


def test_robots_crawl_delay_is_parsed_and_can_stretch_interval():
    policy = RobotsPolicy.parse("User-agent: *\nCrawl-delay: 30\nDisallow: /item\n")
    assert policy.crawl_delay_s == 30
    assert policy.permitted("/shop") is True
    assert policy.permitted("/item") is False
    # 采集间隔取「配置间隔」与「站点明示下限」的较大者
    assert max(timedelta(minutes=2), timedelta(seconds=policy.crawl_delay_s)) == timedelta(minutes=2)
    assert max(timedelta(minutes=2), timedelta(seconds=3600)) == timedelta(seconds=3600)
