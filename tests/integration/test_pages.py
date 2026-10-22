"""页面冒烟：9 个页面 + 健康检查。模板语法错误、上下文缺字段都会在这里炸出来。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


PAGES = ("/", "/targets", "/suggestions", "/tasks", "/products", "/settings")


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(auth_client, path):
    response = auth_client.get(path)
    assert response.status_code == 200, f"{path} 渲染失败"
    assert "竞品监控" in response.content.decode()


def test_health_endpoint_is_public(client):
    response = client.get("/health")
    assert response.status_code in (200, 503)
    payload = response.json()
    assert "status" in payload and "queue" in payload


def test_login_page_renders(client):
    assert client.get("/login").status_code == 200


def test_login_flow_and_lockout(client, admin_user):
    from tenancy.models import LoginThrottle

    bad = client.post("/login", {"email": "admin@a.test", "password": "wrong-pass"})
    assert bad.status_code == 200
    assert "不正确" in bad.content.decode()

    for _ in range(4):
        client.post("/login", {"email": "admin@a.test", "password": "wrong-pass"})
    throttle = LoginThrottle.objects.get(email="admin@a.test")
    assert throttle.locked_until is not None

    locked = client.post("/login", {"email": "admin@a.test", "password": "pw12345678"})
    assert "分钟后再试" in locked.content.decode()


def test_login_success_sets_session(client, admin_user):
    response = client.post("/login", {"email": "admin@a.test", "password": "pw12345678"}, follow=False)
    assert response.status_code == 302
    assert client.get("/").status_code == 200


def test_workbench_first_paint_lists_pending_suggestions(auth_client, tenant):
    """首屏就必须有建议列表：曾经 include 没传 alerts，页面显示「暂无」但计数是 1。"""
    from tenancy.models import Suggestion

    Suggestion.all_objects.create(
        tenant=tenant,
        kind="A_info",
        priority="P0",
        card={"conclusion": "首屏应当看到我", "evidence": [], "action": "a", "owner_role": "operator",
              "deadline": "2026-10-23T12:00:00", "expected_impact": "e", "cost_risk": {"mode": "n/a"},
              "acceptance": "x"},
        event_id=1,
        target_id=1,
    )

    body = auth_client.get("/").content.decode()

    assert "首屏应当看到我" in body
    assert "暂无待处理建议" not in body


def test_target_detail_page(auth_client, binding, baseline_snapshot):
    response = auth_client.get(f"/targets/{binding.id}")
    assert response.status_code == 200
    body = response.content.decode()
    assert "到手价明细" in body


def test_ops_health_requires_platform_ops(auth_client, platform_user, client):
    assert auth_client.get("/ops/health").status_code == 403
    client.force_login(platform_user)
    assert client.get("/ops/health").status_code == 200


def test_index_redirects_platform_ops_to_ops(client, platform_user):
    client.force_login(platform_user)
    response = client.get("/")
    assert response.status_code == 302
    assert response["Location"] == "/ops/health"


def test_add_target_creates_binding(auth_client, tenant):
    from tenancy.models import TenantTarget

    response = auth_client.post(
        "/targets/add",
        {"url": "https://item.taobao.com/item.htm?id=2002", "priority": "core"},
        follow=True,
    )
    assert response.status_code == 200
    assert TenantTarget.all_objects.filter(tenant=tenant, priority="core", enabled=True).count() == 1


def test_add_target_rejects_non_whitelisted_host(auth_client, tenant):
    from tenancy.models import TenantTarget

    response = auth_client.post(
        "/targets/add", {"url": "https://example.com/item?id=1", "priority": "core"}, follow=True
    )
    assert response.status_code == 200
    assert TenantTarget.all_objects.filter(tenant=tenant).count() == 0
    assert "白名单" in response.content.decode()


def test_quota_enforced(auth_client, tenant, target):
    from tenancy.models import TenantTarget

    tenant.quota_json = {"targets": 1}
    tenant.save()
    TenantTarget.all_objects.create(tenant=tenant, target_id=target.id, priority="normal", enabled=True)

    response = auth_client.post(
        "/targets/add", {"url": "https://item.taobao.com/item.htm?id=2003", "priority": "normal"}, follow=True
    )
    assert "上限" in response.content.decode()
