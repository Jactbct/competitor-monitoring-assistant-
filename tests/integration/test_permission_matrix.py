"""权限矩阵：四档角色 × 关键动作。

为什么要写成矩阵而不是零散断言：权限最容易出的不是「全开」，而是**某一条路径漏了检查**。
矩阵把「每一格期望什么」写在测试数据里，加新角色/新动作时按行补一列即可，
评审时也能一眼看出「viewer 到底能不能导出」这种产品问题。

写权限（WRITE_ROLES）= admin / operator；designer 与 viewer 只读。
导出属于只读能力，因此 viewer 也允许 —— 这是有意的产品决定（见设置页说明），
如果哪天改成「导出算敏感操作」，只需把这一格从 200 改成 403，测试会推着代码一起改。
"""

from __future__ import annotations

import pytest

from tenancy.models import Suggestion

pytestmark = pytest.mark.django_db

CARD = {
    "conclusion": "结论",
    "evidence": [{"snapshot_evidence_id": "snap-1", "quote": "引用", "captured_at": "2026-10-22T10:00:00"}],
    "action": "动作",
    "owner_role": "operator",
    "deadline": "2026-10-22T18:00:00",
    "expected_impact": "影响",
    "cost_risk": {"mode": "not_applicable"},
    "acceptance": "验收",
}

ROLES = ("admin", "operator", "designer", "viewer")

#: 动作 → 各角色期望状态码
MATRIX = {
    "accept_suggestion": {"admin": 204, "operator": 204, "designer": 403, "viewer": 403},
    "ignore_suggestion": {"admin": 204, "operator": 204, "designer": 403, "viewer": 403},
    "add_product": {"admin": 302, "operator": 302, "designer": 403, "viewer": 403},
    "add_target": {"admin": 302, "operator": 302, "designer": 403, "viewer": 403},
    "add_member": {"admin": 302, "operator": 302, "designer": 403, "viewer": 403},
    "export_csv": {"admin": 200, "operator": 200, "designer": 200, "viewer": 200},
    "view_settings": {"admin": 200, "operator": 200, "designer": 200, "viewer": 200},
    "ops_health": {"admin": 403, "operator": 403, "designer": 403, "viewer": 403},
}


def _user(role, tenant, request):
    return request.getfixturevalue(f"{role}_user")


def _new_suggestion(tenant) -> Suggestion:
    return Suggestion.all_objects.create(
        tenant=tenant, kind="A_info", priority="P1", card=CARD, event_id=1, target_id=1
    )


def _post(client, action, suggestion) -> int:
    payload = {
        "accept_suggestion": (f"/suggestions/{suggestion.id}/accept", {}),
        "ignore_suggestion": (f"/suggestions/{suggestion.id}/ignore", {"reason_tag": "价格无压力"}),
        "add_product": ("/products/add", {"sku": "SKU-X", "price_display": "99.00", "cost": "60.00"}),
        "add_target": ("/targets/add", {"url": "https://item.taobao.com/item.htm?id=9999", "priority": "normal"}),
        "add_member": (
            "/settings/members/add",
            {"email": "new@a.test", "password": "pw12345678", "role": "operator"},
        ),
    }[action]
    path, data = payload
    return client.post(path, data).status_code


@pytest.mark.parametrize("action", sorted(MATRIX))
@pytest.mark.parametrize("role", ROLES)
def test_permission_matrix(action, role, client, tenant, request):
    user = _user(role, tenant, request)
    client.force_login(user)
    expected = MATRIX[action][role]

    if action in {"export_csv", "view_settings", "ops_health"}:
        path = {"export_csv": "/suggestions/export.csv", "view_settings": "/settings", "ops_health": "/ops/health"}[action]
        status = client.get(path).status_code
    else:
        status = _post(client, action, _new_suggestion(tenant))

    assert status == expected, f"{role} 执行 {action} 期望 {expected}，实际 {status}"


def test_only_admin_can_add_member(client, tenant, admin_user, operator_user):
    """写权限给了 operator，但「加成员」应只允许管理员 —— 状态码相同，靠消息区分。"""
    operator_user.role = "operator"
    operator_user.save(update_fields=["role"])

    client.force_login(operator_user)
    response = client.post(
        "/settings/members/add",
        {"email": "byop@a.test", "password": "pw12345678", "role": "operator"},
        follow=True,
    )
    assert "只有管理员" in response.content.decode()

    client.force_login(admin_user)
    response = client.post(
        "/settings/members/add",
        {"email": "byadmin@a.test", "password": "pw12345678", "role": "operator"},
        follow=True,
    )
    assert "已添加成员" in response.content.decode()


def test_platform_ops_cannot_touch_tenant_business(client, platform_user, tenant):
    client.force_login(platform_user)
    suggestion = _new_suggestion(tenant)

    assert client.post(f"/suggestions/{suggestion.id}/accept").status_code == 403
    assert client.post("/targets/add", {"url": "https://item.taobao.com/item.htm?id=8"}).status_code == 403
    assert client.get("/suggestions/export.csv").status_code == 403
