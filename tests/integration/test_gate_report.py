"""判据报告：数字必须能跑出来，且能作为门禁（--gate 不达标即退出 1）。"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from shared.models import Snapshot
from tenancy.models import Suggestion

pytestmark = pytest.mark.django_db

FULL_CARD = {
    "conclusion": "竞品降价",
    "evidence": [{"snapshot_evidence_id": "snap-x", "quote": "满129减15", "captured_at": "2026-10-22T10:00:00"}],
    "action": "跟价",
    "owner_role": "operator",
    "deadline": "2026-10-22T18:00:00",
    "expected_impact": "恢复转化",
    "cost_risk": {"mode": "not_applicable"},
    "acceptance": "到手价对齐",
}


def _snap(target, certain: bool, evidence_id: str):
    return Snapshot.objects.create(
        target=target,
        captured_at=timezone.now() - timedelta(hours=1),
        http_status=200,
        content_sha256="e" * 64,
        fields={"title": "x"},
        field_completeness="1.000",
        price_display="129.00",
        price_final="114.00" if certain else None,
        price_confidence="certain" if certain else "uncertain",
        evidence_id=evidence_id,
        status=Snapshot.STATUS_VALID,
    )


def _suggestion(tenant, card=None, degraded=False):
    return Suggestion.all_objects.create(
        tenant=tenant,
        kind="A_info",
        priority="P1",
        card=card or FULL_CARD,
        event_id=1,
        target_id=1,
        degraded=degraded,
    )


def _run(*args) -> str:
    out = StringIO()
    call_command("gate_report", *args, stdout=out)
    return out.getvalue()


def test_report_without_samples_says_so():
    output = _run("--days", "7")
    assert "样本不足" in output


def test_ratio_below_threshold_blocks_b_class(target):
    _snap(target, True, "snap-c1")
    _snap(target, False, "snap-u1")

    output = _run("--days", "7")

    assert "50.0%" in output
    assert "未达" in output
    assert "还需 1 条可确定到手价" in output


def test_ratio_above_threshold_opens_b_class(target, tenant):
    for i in range(4):
        _snap(target, True, f"snap-c{i}")
    _snap(target, True, "snap-x")  # 卡片引用的证据必须真的存在，否则证据回链率必然 FAIL
    _suggestion(tenant)

    output = _run("--days", "7")

    assert "可打开" in output
    assert "全部硬门槛通过" in output


def test_degraded_rate_is_counted(target, tenant):
    _snap(target, True, "snap-c1")
    for _ in range(4):
        _suggestion(tenant, degraded=True)

    output = _run("--days", "7")
    assert "分析降级率" in output
    assert "100.0%" in output


def test_incomplete_card_fails_eight_field_gate(target, tenant):
    _snap(target, True, "snap-c1")
    broken = dict(FULL_CARD)
    broken.pop("acceptance")
    _suggestion(tenant, card=broken)

    with pytest.raises(SystemExit) as exc:
        _run("--days", "7", "--gate")

    assert exc.value.code == 1


def test_evidence_link_rate_detects_missing_snapshot(target, tenant):
    _snap(target, True, "snap-exists")
    card = dict(FULL_CARD)
    card["evidence"] = [{"snapshot_evidence_id": "snap-missing", "quote": "引用", "captured_at": "2026-10-22T10:00:00"}]
    _suggestion(tenant, card=card)

    output = _run("--days", "7")

    assert "证据回链率" in output
    assert "0.0%" in output
