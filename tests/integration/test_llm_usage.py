"""租户级 LLM 用量与配额。

关键断言：**一个租户把额度跑满，不影响另一个租户**。
全局口径的实现最省事，但在 SaaS 里等于让大客户把所有人的模型调用一起拖停。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from core.config import LlmSettings
from core.tenant import tenant_scope
from infra.llm.client import OpenAiCompatibleLlm, QuotaExceeded
from tenancy.models import LlmUsage

pytestmark = pytest.mark.django_db


def _client(quota: int = 100) -> OpenAiCompatibleLlm:
    llm = OpenAiCompatibleLlm.__new__(OpenAiCompatibleLlm)
    llm.llm = LlmSettings(
        mode="openai",
        api_key="test-key",
        base_url="http://127.0.0.1:9",  # 不可达：配额检查在 HTTP 之前，正常路径不会真发请求
        daily_token_quota=quota,
    )
    return llm


def _usage(tenant, tokens_in=0, tokens_out=0, day=None):
    return LlmUsage.all_objects.create(
        tenant=tenant,
        day=day or timezone.localdate(),
        node="n2",
        calls=1,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=Decimal("0.0010"),
    )


def test_usage_accumulates_per_node(tenant):
    llm = _client()
    llm._record_usage(str(tenant.id), "n1", 100, 20, Decimal("0.01"))
    llm._record_usage(str(tenant.id), "n1", 50, 10, Decimal("0.005"))
    llm._record_usage(str(tenant.id), "n2", 30, 5, Decimal("0.002"))

    rows = {row.node: row for row in LlmUsage.all_objects.filter(tenant=tenant)}
    assert rows["n1"].calls == 2
    assert rows["n1"].tokens_in == 150
    assert rows["n1"].tokens == 180  # 150 in + 30 out
    assert rows["n2"].calls == 1
    assert rows["n1"].cost == Decimal("0.0150")


def test_tokens_used_today_is_per_tenant(tenant, tenant_b):
    _usage(tenant, tokens_in=80, tokens_out=20)
    _usage(tenant_b, tokens_in=1000, tokens_out=999)

    llm = _client()
    assert llm._tokens_used_today(str(tenant.id)) == 100
    assert llm._tokens_used_today(str(tenant_b.id)) == 1999
    assert llm._tokens_used_today(None) == 0  # 无租户上下文不限


def test_yesterday_usage_does_not_count_today(tenant):
    _usage(tenant, tokens_in=999, day=timezone.localdate() - timedelta(days=1))
    assert _client()._tokens_used_today(str(tenant.id)) == 0


def test_exhausted_tenant_is_rejected_before_http(tenant):
    _usage(tenant, tokens_in=100, tokens_out=50)  # 已超 100 的配额
    llm = _client(quota=100)

    with pytest.raises(QuotaExceeded):
        llm.complete_json(
            node="n1", system="s", user="u", model="small", cache_material="m-" + str(tenant.id),
            tenant_id=str(tenant.id),
        )


def test_other_tenant_is_unaffected_by_exhausted_one(tenant, tenant_b):
    """这条是整个改动的理由：A 打满额度，B 仍然能正常走（不抛 QuotaExceeded）。"""
    _usage(tenant, tokens_in=500, tokens_out=500)

    llm = _client(quota=100)
    assert llm._tokens_used_today(str(tenant.id)) >= 100
    assert llm._tokens_used_today(str(tenant_b.id)) == 0


def test_reserved_quota_zero_means_unlimited(tenant):
    _usage(tenant, tokens_in=10_000, tokens_out=10_000)
    llm = _client(quota=0)
    # quota=0 时不做拒绝（配置成 0 表示「不限」）
    assert llm._tokens_used_today(str(tenant.id)) == 20_000


def test_usage_rows_are_isolated_by_tenant_scope(tenant, tenant_b):
    _usage(tenant, tokens_in=10)
    _usage(tenant_b, tokens_in=20)

    with tenant_scope(str(tenant.id)):
        rows = list(LlmUsage.objects.all())

    assert len(rows) == 1
    assert rows[0].tenant_id == tenant.id


def test_settings_page_shows_usage(auth_client, tenant):
    _usage(tenant, tokens_in=1200, tokens_out=300)

    response = auth_client.get("/settings")

    assert response.status_code == 200
    body = response.content.decode()
    assert "今日模型用量" in body
    assert "1500" in body  # 1200 + 300
