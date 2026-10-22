"""审查发现的回归用例（B2 / B4 / M9 / M10 / M11 / M12 / M13）。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from jobs.runner import Worker
from shared.models import Job, Target
from tenancy.models import AppUser, Suggestion, TenantTarget

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


# ---------------- B2：熔断到期后必须重新进调度 ----------------
def test_expired_circuit_is_rescheduled_and_marked_half_open(target, binding):
    from infra import budget
    from jobs.handlers.collect import handle_schedule_tick

    target.circuit_state = Target.CIRCUIT_OPEN
    target.circuit_until = timezone.now() - timedelta(minutes=1)  # 冷却已过
    target.next_due_at = timezone.now() - timedelta(minutes=1)
    target.save(update_fields=["circuit_state", "circuit_until", "next_due_at"])

    budget.refill_now()
    result = handle_schedule_tick({"limit": 10})

    assert result["enqueued"] == 1
    target.refresh_from_db()
    assert target.circuit_state == Target.CIRCUIT_HALF_OPEN


def test_open_circuit_within_cooldown_is_not_scheduled(target, binding):
    from infra import budget
    from jobs.handlers.collect import handle_schedule_tick

    target.circuit_state = Target.CIRCUIT_OPEN
    target.circuit_until = timezone.now() + timedelta(minutes=30)
    target.next_due_at = timezone.now() - timedelta(minutes=1)
    target.save(update_fields=["circuit_state", "circuit_until", "next_due_at"])

    budget.refill_now()
    assert handle_schedule_tick({"limit": 10})["enqueued"] == 0


# ---------------- B4：周期任务真的被投递 ----------------
def test_periodic_tick_enqueues_health_and_silence():
    worker = Worker(concurrency=1, with_scheduler=True)
    worker._maybe_periodic()

    kinds = set(Job.objects.values_list("kind", flat=True))
    assert {"health_snapshot", "silence_check"} <= kinds


def test_periodic_tick_is_idempotent_within_hour():
    worker = Worker(concurrency=1, with_scheduler=True)
    worker._maybe_periodic()
    before = Job.objects.count()

    worker._last_periodic = 0.0
    worker._maybe_periodic()

    assert Job.objects.count() == before  # 幂等键挡住重复投递


def test_daily_digest_only_enqueued_at_configured_hour(settings):
    from core.config import get_settings

    worker = Worker(concurrency=1, with_scheduler=True)
    hour = get_settings().notify.digest_hour
    local = timezone.localtime()
    if local.hour == hour:
        pytest.skip("当前正好是日报小时，两个分支无法区分")

    worker._maybe_periodic()
    assert not Job.objects.filter(kind="daily_digest").exists()


# ---------------- M9：角色白名单 ----------------
def test_cannot_escalate_to_platform_ops_via_member_form(client, tenant, admin_user):
    client.force_login(admin_user)
    response = client.post(
        "/settings/members/add",
        {"email": "evil@a.test", "password": "pw12345678", "role": "platform_ops"},
        follow=True,
    )

    assert "角色不合法" in response.content.decode()
    assert not AppUser.objects.filter(email="evil@a.test").exists()


def test_invalid_role_string_is_rejected(client, tenant, admin_user):
    client.force_login(admin_user)
    client.post(
        "/settings/members/add",
        {"email": "weird@a.test", "password": "pw12345678", "role": "superuser"},
        follow=True,
    )
    assert not AppUser.objects.filter(email="weird@a.test").exists()


# ---------------- M10：开放重定向 ----------------
def test_login_next_cannot_redirect_to_external_host(client, admin_user):
    response = client.post(
        "/login?next=//evil.example.com/steal",
        {"email": "admin@a.test", "password": "pw12345678"},
    )
    assert response.status_code == 302
    assert response["Location"] == "/"


# ---------------- M11：CSV 公式注入 ----------------
def test_csv_export_neutralises_formula_prefix(client, tenant, admin_user):
    client.force_login(admin_user)
    card = dict(CARD)
    card["conclusion"] = "=HYPERLINK(\"http://evil\")"
    Suggestion.all_objects.create(
        tenant=tenant, kind="A_info", priority="P1", card=card, event_id=1, target_id=1
    )

    import csv
    import io

    body = client.get("/suggestions/export.csv").content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(body)))
    data_row = rows[2]
    assert data_row[3].startswith("'="), "公式前缀必须被转义（加单引号）"


# ---------------- M12：原始页路径穿越 ----------------
def test_read_raw_rejects_path_traversal(tmp_path, monkeypatch):
    import gzip

    from infra import storage

    monkeypatch.setattr(storage, "_root", lambda: tmp_path)
    with gzip.open(tmp_path / "ok.html.gz", "wb") as fh:
        fh.write(b"hello")

    assert storage.read_raw("../../../etc/passwd") is None
    assert storage.read_raw("/etc/passwd") is None
    assert storage.read_raw("ok.html.gz") == b"hello"


# ---------------- M13：可空唯一列必须真正唯一 ----------------
def test_duplicate_binding_with_null_product_is_rejected(tenant, target):
    """NULLS NOT DISTINCT 是 PG 特性；SQLite 开发库不生成该子句，故仅在 PG 上断言。"""
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("SQLite 后端不生成 NULLS NOT DISTINCT 子句（生产为 PG）")
    TenantTarget.all_objects.create(tenant=tenant, target_id=target.id, priority="normal", enabled=True)

    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        TenantTarget.all_objects.create(
            tenant=tenant, target_id=target.id, priority="core", enabled=True
        )
