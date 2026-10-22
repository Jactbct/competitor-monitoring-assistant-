"""测试夹具。

约定：
* 价格类断言一律用 Decimal，不用 float；
* 每个涉及租户的测试都显式建租户，避免默认管理器因无上下文返回 0 行造成「假绿」；
* 默认跑 SQLite；要跑 PG 用 ``PG_TESTS=1 DATABASE_URL=postgresql://postgres:... pytest``。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from shared.models import Snapshot, Target
from tenancy.models import AppUser, SelfProduct, Tenant, TenantTarget

PASSWORD = "pw12345678"


@pytest.fixture
def tenant(db) -> Tenant:
    return Tenant.objects.create(name="租户A", plan="pro", quota_json={"targets": 100})


@pytest.fixture
def tenant_b(db) -> Tenant:
    return Tenant.objects.create(name="租户B", plan="basic", quota_json={"targets": 10})


@pytest.fixture
def admin_user(tenant) -> AppUser:
    return AppUser.objects.create_user(email="admin@a.test", password=PASSWORD, tenant=tenant, role="admin")


@pytest.fixture
def operator_user(tenant) -> AppUser:
    return AppUser.objects.create_user(email="op@a.test", password=PASSWORD, tenant=tenant, role="operator")


@pytest.fixture
def designer_user(tenant) -> AppUser:
    return AppUser.objects.create_user(email="design@a.test", password=PASSWORD, tenant=tenant, role="designer")


@pytest.fixture
def viewer_user(tenant) -> AppUser:
    return AppUser.objects.create_user(email="view@a.test", password=PASSWORD, tenant=tenant, role="viewer")


@pytest.fixture
def platform_user(db) -> AppUser:
    return AppUser.objects.create_user(
        email="platform@test", password=PASSWORD, tenant=None, role="platform_ops"
    )


@pytest.fixture
def auth_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def product(tenant) -> SelfProduct:
    return SelfProduct.all_objects.create(
        tenant=tenant,
        sku="TS-001",
        title="纯棉短袖T恤男 夏季 宽松",
        price_display="139.00",
        cost="78.00",
        margin_floor="0.22",
    )


@pytest.fixture
def target(db) -> Target:
    return Target.objects.create(
        platform="taobao",
        url="https://item.taobao.com/item.htm?id=1001",
        url_hash="a" * 64,
        next_due_at=timezone.now(),
        interval_min=120,
    )


@pytest.fixture
def binding(tenant, target, product) -> TenantTarget:
    return TenantTarget.all_objects.create(
        tenant=tenant, target_id=target.id, self_product=product, priority="core", enabled=True
    )


@pytest.fixture
def baseline_snapshot(target) -> Snapshot:
    return Snapshot.objects.create(
        target=target,
        captured_at=timezone.now() - timedelta(hours=3),
        http_status=200,
        content_sha256="b" * 64,
        extract_version="rules-1.0",
        fields={
            "title": "纯棉短袖T恤男夏季宽松五分袖打底衫",
            "price_display": "149.00",
            "price_final": "149.00",
            "promo_signature": "none",
            "main_image": "//img/a.jpg",
            "sku_count": 4,
            "attrs": {"品牌": "简衣"},
        },
        field_completeness="1.000",
        price_display="149.00",
        price_final="149.00",
        price_confidence="certain",
        promo_detail=[],
        evidence_id="snap-base0001",
        status=Snapshot.STATUS_VALID,
    )


class FakeLlm:
    """可编程的假 LLM，用来测「模型乱说时系统怎么兜住」。"""

    def __init__(self, responses: list[dict] | dict) -> None:
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls = 0

    def complete_json(self, *, node, system, user, model, cache_material, tenant_id=None):
        from domain.analyze.ports import LlmResult

        data = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(data, Exception):
            raise data
        return LlmResult(data=data, model="fake", node=node, tokens_in=10, tokens_out=5)


@pytest.fixture
def fake_llm():
    return FakeLlm
