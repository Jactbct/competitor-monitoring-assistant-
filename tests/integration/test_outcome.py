"""效果回看（T+7）：采纳 → 建回看记录 → 到期判定 → 报表。

这是「建议 → 执行 → 结果」闭环的最后一环。没有它，产品只能证明「运营点了按钮」，
证明不了「建议有用」。
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from jobs.handlers.outcome import classify_change, handle_outcome_check
from shared.models import Snapshot
from tenancy.models import Outcome, Suggestion

pytestmark = pytest.mark.django_db

CARD = {
    "conclusion": "竞品降价，建议让利 25 元",
    "evidence": [{"snapshot_evidence_id": "snap-1", "quote": "满129减15", "captured_at": "2026-10-22T10:00:00"}],
    "action": "发放店铺券，到手价对齐 114 元",
    "owner_role": "operator",
    "deadline": "2026-10-22T18:00:00",
    "expected_impact": "恢复转化率",
    "cost_risk": {"mode": "not_applicable"},
    "acceptance": "券上线后到手价 = 114 元",
}


def _suggestion(tenant, target) -> Suggestion:
    return Suggestion.all_objects.create(
        tenant=tenant,
        kind="B_promo",
        priority="P0",
        card=CARD,
        event_id=1,
        target_id=target.id,
        status=Suggestion.STATUS_PENDING,
    )


def _price_snapshot(target, price: str) -> Snapshot:
    return Snapshot.objects.create(
        target=target,
        captured_at=timezone.now(),
        http_status=200,
        content_sha256="f" * 64,
        fields={"title": "竞品"},
        field_completeness="1.000",
        price_display=price,
        price_final=price,
        price_confidence="certain",
        evidence_id=f"snap-{price}",
        status=Snapshot.STATUS_VALID,
    )


# ---------------- 采纳时建回看记录 ----------------
def test_accept_schedules_outcome(client, tenant, target, product, admin_user):
    from jobs.queue import Job

    target.last_price_final = "114.00"
    target.last_price_display = "129.00"
    target.save(update_fields=["last_price_final", "last_price_display"])
    suggestion = _suggestion(tenant, target)
    client.force_login(admin_user)

    client.post(f"/suggestions/{suggestion.id}/accept")

    outcome = Outcome.all_objects.get(suggestion=suggestion)
    assert outcome.outcome == Outcome.PENDING
    assert str(outcome.price_at_accept) == "114.00"
    assert 6 <= (outcome.check_due_at - timezone.now()).days <= 8

    job = Job.objects.filter(kind="outcome_check").first()
    assert job is not None
    assert job.run_after >= timezone.now() + timedelta(days=6)


def test_accept_twice_keeps_one_outcome(client, tenant, target, product, admin_user):
    suggestion = _suggestion(tenant, target)
    client.force_login(admin_user)
    client.post(f"/suggestions/{suggestion.id}/accept")
    client.post(f"/suggestions/{suggestion.id}/accept")  # 重复点击

    assert Outcome.all_objects.filter(suggestion=suggestion).count() == 1


# ---------------- 判定口径 ----------------
@pytest.mark.parametrize(
    ("change", "verdict"),
    [
        ("0.05", "price_reverted"),
        ("0.0101", "price_reverted"),
        ("0.005", "price_kept"),
        ("-0.005", "price_kept"),
        ("-0.0101", "price_further_down"),
        ("-0.30", "price_further_down"),
        (None, "unavailable"),
    ],
)
def test_classify_change(change, verdict):
    from decimal import Decimal

    assert classify_change(Decimal(change) if change is not None else None) == verdict


# ---------------- handler 端到端 ----------------
def test_handler_marks_price_reverted(tenant, target, product, admin_user):
    suggestion = _suggestion(tenant, target)
    target.last_price_final = "100.00"
    target.save(update_fields=["last_price_final"])
    outcome = Outcome.all_objects.create(
        tenant=tenant,
        suggestion=suggestion,
        target_id=target.id,
        accepted_at=timezone.now() - timedelta(days=7),
        check_due_at=timezone.now(),
        price_at_accept="100.00",
    )
    _price_snapshot(target, "110.00")  # 竞品回涨 10%

    result = handle_outcome_check({"outcome_id": str(outcome.id)})

    assert result["verdict"] == "price_reverted"
    outcome.refresh_from_db()
    assert outcome.measured_at is not None
    assert str(outcome.change_pct) == "0.1000"
    assert "回涨" in outcome.note


def test_handler_marks_further_down(tenant, target, product):
    suggestion = _suggestion(tenant, target)
    outcome = Outcome.all_objects.create(
        tenant=tenant,
        suggestion=suggestion,
        target_id=target.id,
        accepted_at=timezone.now() - timedelta(days=7),
        check_due_at=timezone.now(),
        price_at_accept="100.00",
    )
    _price_snapshot(target, "80.00")

    handle_outcome_check({"outcome_id": str(outcome.id)})

    outcome.refresh_from_db()
    assert outcome.outcome == Outcome.PRICE_FURTHER_DOWN


def test_handler_marks_unavailable_without_price(tenant, target, product):
    suggestion = _suggestion(tenant, target)
    outcome = Outcome.all_objects.create(
        tenant=tenant,
        suggestion=suggestion,
        target_id=target.id,
        accepted_at=timezone.now() - timedelta(days=7),
        check_due_at=timezone.now(),
        price_at_accept="100.00",
    )
    # 没有任何快照 → 不猜
    handle_outcome_check({"outcome_id": str(outcome.id)})

    outcome.refresh_from_db()
    assert outcome.outcome == Outcome.UNAVAILABLE
    assert outcome.price_after is None


def test_handler_is_idempotent(tenant, target, product):
    suggestion = _suggestion(tenant, target)
    outcome = Outcome.all_objects.create(
        tenant=tenant,
        suggestion=suggestion,
        target_id=target.id,
        accepted_at=timezone.now() - timedelta(days=7),
        check_due_at=timezone.now(),
        price_at_accept="100.00",
    )
    _price_snapshot(target, "100.00")

    first = handle_outcome_check({"outcome_id": str(outcome.id)})
    second = handle_outcome_check({"outcome_id": str(outcome.id)})

    assert first["verdict"] == "price_kept"
    assert second["skipped"] == "already_measured"


# ---------------- 界面与报表 ----------------
def test_panel_shows_outcome(client, tenant, target, product, admin_user):
    suggestion = _suggestion(tenant, target)
    Outcome.all_objects.create(
        tenant=tenant,
        suggestion=suggestion,
        target_id=target.id,
        accepted_at=timezone.now(),
        check_due_at=timezone.now() + timedelta(days=7),
        price_at_accept="114.00",
    )
    client.force_login(admin_user)

    body = client.get(f"/suggestions/{suggestion.id}/panel").content.decode()

    assert "效果回看" in body
    assert "待回看" in body


def test_workbench_shows_outcome_summary(client, tenant, target, product, admin_user):
    suggestion = _suggestion(tenant, target)
    Outcome.all_objects.create(
        tenant=tenant,
        suggestion=suggestion,
        target_id=target.id,
        accepted_at=timezone.now() - timedelta(days=8),
        check_due_at=timezone.now(),
        measured_at=timezone.now(),
        price_at_accept="100.00",
        price_after="105.00",
        change_pct="0.0500",
        outcome=Outcome.PRICE_REVERTED,
    )
    client.force_login(admin_user)

    body = client.get("/").content.decode()
    assert "7 天回看" in body


def test_outcome_report_runs(tenant, target, product):
    suggestion = _suggestion(tenant, target)
    Outcome.all_objects.create(
        tenant=tenant,
        suggestion=suggestion,
        target_id=target.id,
        accepted_at=timezone.now() - timedelta(days=8),
        check_due_at=timezone.now(),
        measured_at=timezone.now(),
        price_at_accept="100.00",
        price_after="104.00",
        change_pct="0.0400",
        outcome=Outcome.PRICE_REVERTED,
    )
    out = StringIO()
    call_command("outcome_report", stdout=out)
    text = out.getvalue()
    assert "价格回涨" in text
    assert "回涨占比过半" in text
