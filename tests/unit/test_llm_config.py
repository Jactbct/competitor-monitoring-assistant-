"""模型接入的两处「配置容错」：地址归一化 + 不支持 response_format 的网关。

这两条都是接第三方网关时的真实坑：
* 供应商文档给完整 endpoint，客户端又拼一次 → `.../chat/completions/chat/completions`
* OpenAI 兼容但不完整：不支持 `response_format`，一律 400
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest

from core.config import LlmSettings
from infra.llm.client import LlmError, OpenAiCompatibleLlm


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://nodlink.cn/v1/chat/completions", "https://nodlink.cn/v1"),
        ("https://nodlink.cn/v1/chat/completions/", "https://nodlink.cn/v1"),
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        ("https://api.openai.com/v1/", "https://api.openai.com/v1"),
        ("  https://host/v1/chat/completions  ", "https://host/v1"),
        ("", ""),
    ],
)
def test_base_url_normalisation(raw, expected):
    assert LlmSettings.normalize_base_url(raw) == expected


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    """按顺序返回预设响应，并记录每次请求的 payload。"""

    sent: list[dict] = []
    responses: list[_FakeResponse] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        # 必须深拷贝：客户端会在重试时原地 pop("response_format")，
        # 直接存引用会导致两次记录看到的是同一份（已被改过的）payload。
        _FakeClient.sent.append({"url": url, "payload": copy.deepcopy(json)})
        return _FakeClient.responses.pop(0)


@pytest.fixture
def llm(monkeypatch):
    from infra.llm import client as module

    monkeypatch.setattr(module.httpx, "Client", _FakeClient)
    _FakeClient.sent = []
    instance = OpenAiCompatibleLlm.__new__(OpenAiCompatibleLlm)
    instance.llm = LlmSettings(
        mode="openai",
        api_key="k",
        base_url="https://nodlink.cn/v1",
        model_extract="gpt-4o-mini",
        model_reason="gpt-4o-mini",
        daily_token_quota=0,
    )
    # 绕开缓存与记账（它们要 DB）
    monkeypatch.setattr(instance, "_read_cache", lambda key: None)
    monkeypatch.setattr(instance, "_write_cache", lambda *a, **k: None)
    monkeypatch.setattr(instance, "_record_tokens", lambda *a, **k: None)
    monkeypatch.setattr(instance, "_record_usage", lambda *a, **k: None)
    return instance


def _ok_body(data: dict) -> dict:
    return {
        "choices": [{"message": {"content": "```json\n" + json.dumps(data) + "\n```"}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8},
    }


def test_url_is_built_once_even_if_user_pasted_full_endpoint(llm):
    _FakeClient.responses = [_FakeResponse(200, _ok_body({"ok": True}))]

    llm.complete_json(
        node="n1", system="s", user="u", model="gpt-4o-mini", cache_material="m1"
    )

    assert _FakeClient.sent[0]["url"] == "https://nodlink.cn/v1/chat/completions"


def test_retries_without_response_format_when_gateway_rejects_it(llm):
    _FakeClient.responses = [
        _FakeResponse(400, {"error": {"message": "response_format is not supported"}}),
        _FakeResponse(200, _ok_body({"ok": True, "echo": "ping"})),
    ]

    result = llm.complete_json(
        node="n1", system="s", user="u", model="gpt-4o-mini", cache_material="m2"
    )

    assert result.data["ok"] is True
    assert "response_format" in _FakeClient.sent[0]["payload"]
    assert "response_format" not in _FakeClient.sent[1]["payload"]
    assert result.tokens_in == 12 and result.tokens_out == 8
    assert isinstance(result.cost, Decimal)


def test_401_is_fatal_and_not_retried(llm):
    _FakeClient.responses = [_FakeResponse(401, {"error": {"message": "invalid api key"}})]

    with pytest.raises(LlmError):
        llm.complete_json(
            node="n1", system="s", user="u", model="gpt-4o-mini", cache_material="m3"
        )

    assert len(_FakeClient.sent) == 1  # 只打了一次，没有无意义的重复
