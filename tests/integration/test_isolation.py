"""多租户隔离 —— 6 条越权用例（架构文档 §9.2 M0 退出条件：全绿才允许进 M1）。

说明：SQLite 下没有 RLS，第 3/4 条验证的是**默认管理器**的 fail-closed 行为；
PG 环境下同一组断言由 RLS 兜底（见 tenancy/migrations/0002_row_level_security.py）。
"""

from __future__ import annotations

import pytest

from core.tenant import tenant_scope
from tenancy.models import AuditLog, Suggestion

pytestmark = pytest.mark.django_db

CARD = {
    "conclusion": "测试结论",
    "evidence": [{"snapshot_evidence_id": "snap-1", "quote": "引用", "captured_at": "2026-10-21T10:00:00"}],
    "action": "动作",
    "owner_role": "operator",
    "deadline": "2026-10-21T18:00:00",
    "expected_impact": "影响",
    "cost_risk": {"mode": "not_applicable"},
    "acceptance": "验收",
}


def _suggestion(tenant, priority="P1") -> Suggestion:
    return Suggestion.all_objects.create(
        tenant=tenant, kind="A_info", priority=priority, card=CARD, event_id=1, target_id=1
    )


def test_case1_cross_tenant_object_is_404_and_audited(client, admin_user, tenant_b):
    other = _suggestion(tenant_b)
    client.force_login(admin_user)

    response = client.get(f"/suggestions/{other.id}/panel")

    assert response.status_code == 404  # 不是 403：不暴露资源存在性
    assert AuditLog.objects.filter(action="tenant.cross_access_blocked").exists()


def test_case2_tenant_id_in_params_is_400(client, admin_user, tenant_b):
    client.force_login(admin_user)

    response = client.get("/targets", {"tenant_id": str(tenant_b.id)})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "TENANT_MISSING_CONTEXT"
    assert AuditLog.objects.filter(action="tenant.cross_access_blocked").exists()


def test_case3_orm_scope_hides_other_tenant(client, admin_user, tenant, tenant_b):
    _suggestion(tenant)
    _suggestion(tenant_b)

    with tenant_scope(str(tenant.id)):
        rows = list(Suggestion.objects.all())

    assert len(rows) == 1
    assert rows[0].tenant_id == tenant.id


def test_case4_without_context_everything_is_denied(client, admin_user, tenant):
    _suggestion(tenant)
    assert Suggestion.objects.count() == 0  # fail-closed，不是「返回全部」


def test_case5_platform_ops_cannot_read_business_pages(client, platform_user):
    client.force_login(platform_user)

    for path in ("/suggestions", "/targets", "/products", "/tasks", "/settings"):
        response = client.get(path)
        assert response.status_code == 403, f"{path} 允许平台运营访问"


def test_case6_export_has_watermark_and_audit(client, admin_user, tenant):
    _suggestion(tenant)
    client.force_login(admin_user)

    response = client.get("/suggestions/export.csv")

    assert response.status_code == 200
    body = response.content.decode("utf-8-sig")
    assert tenant.name in body
    assert admin_user.email in body
    assert "导出水印" in body
    assert AuditLog.objects.filter(action="suggestion.exported").exists()


def test_viewer_role_cannot_write(client, viewer_user, tenant):
    row = _suggestion(tenant)
    client.force_login(viewer_user)

    response = client.post(f"/suggestions/{row.id}/accept")

    assert response.status_code == 403
    row.refresh_from_db()
    assert row.status == "pending"


@pytest.mark.django_db
def test_accept_creates_task_and_feedback(client, admin_user, tenant):
    from tenancy.models import Feedback, Task

    row = _suggestion(tenant, priority="P0")
    client.force_login(admin_user)

    response = client.post(f"/suggestions/{row.id}/accept")

    assert response.status_code == 204
    row.refresh_from_db()
    assert row.status == Suggestion.STATUS_ACCEPTED
    assert Task.all_objects.filter(suggestion=row).count() == 1
    assert Feedback.all_objects.filter(suggestion=row, verdict="accepted").count() == 1


def test_ignore_requires_reason_tag(client, admin_user, tenant):
    row = _suggestion(tenant)
    client.force_login(admin_user)

    response = client.post(f"/suggestions/{row.id}/ignore", {"reason_tag": ""})

    assert response.status_code == 200  # 回渲染面板并带错误
    row.refresh_from_db()
    assert row.status == "pending"
    assert "原因" in response.content.decode()
