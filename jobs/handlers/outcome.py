"""效果回看 handler（T+7）。

「采纳」如果只停在点击，产品就无法回答「建议到底有没有用」。
这里在采纳 7 天后取一次竞品当前价，与采纳时的价格对比，给出四种确定性结论之一。

判定阈值（宁可保守，不要制造虚假的「有效」）：
* 回涨 ≥ 1% → 原降价是短期活动（``price_reverted``）
* 变化在 ±1% 内 → 价格维持（``price_kept``）
* 继续下探 ≥ 1% → 仍在下跌（``price_further_down``）
* 取不到价 → ``unavailable``（不猜、不当作 0）
"""

from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from core.db import tenant_atomic
from core.log import get_logger
from core.tenant import tenant_scope
from shared.models import Snapshot, Target
from tenancy.models import Outcome

__all__ = ["classify_change", "handle_outcome_check"]

log = get_logger("outcome")

REVERT_THRESHOLD = Decimal("0.01")


def classify_change(change_pct: Decimal | None) -> str:
    if change_pct is None:
        return Outcome.UNAVAILABLE
    if change_pct <= -REVERT_THRESHOLD:
        return Outcome.PRICE_FURTHER_DOWN
    if change_pct >= REVERT_THRESHOLD:
        return Outcome.PRICE_REVERTED
    return Outcome.PRICE_KEPT


def handle_outcome_check(payload: dict) -> dict:
    outcome_id = str(payload["outcome_id"])
    outcome = Outcome.all_objects.filter(pk=outcome_id).first()
    if outcome is None:
        return {"skipped": "outcome_missing"}
    if outcome.measured_at is not None:
        return {"skipped": "already_measured"}

    now = timezone.now()
    target = Target.objects.filter(pk=outcome.target_id).first() if outcome.target_id else None
    snapshot = None
    if target is not None:
        snapshot = (
            Snapshot.objects.filter(target=target, status=Snapshot.STATUS_VALID)
            .order_by("-captured_at")
            .first()
        )

    price_after = None
    if snapshot is not None:
        price_after = snapshot.price_final or snapshot.price_display

    before = outcome.price_at_accept
    change_pct: Decimal | None = None
    note = ""
    if before is None or before <= 0:
        verdict = Outcome.UNAVAILABLE
        note = "采纳时未取到竞品价格，无法回看"
    elif price_after is None:
        verdict = Outcome.UNAVAILABLE
        note = "回看时未取到竞品价格"
    else:
        change_pct = ((price_after - before) / before).quantize(Decimal("0.0001"))
        verdict = classify_change(change_pct)
        note = {
            Outcome.PRICE_REVERTED: "竞品价格已回涨，本次让利可能不必长期维持",
            Outcome.PRICE_KEPT: "竞品价格维持，建议仍然有效",
            Outcome.PRICE_FURTHER_DOWN: "竞品继续下探，需重新评估跟价幅度",
        }.get(verdict, "")

    with tenant_scope(str(outcome.tenant_id)), tenant_atomic(str(outcome.tenant_id)):
        Outcome.all_objects.filter(pk=outcome.pk).update(
            measured_at=now,
            price_after=price_after,
            change_pct=change_pct,
            outcome=verdict,
            note=note,
        )

    log.info(
        "outcome.measured",
        extra={
            "outcome_id": outcome_id,
            "verdict": verdict,
            "before": str(before),
            "after": str(price_after),
        },
    )
    return {
        "outcome_id": outcome_id,
        "verdict": verdict,
        "change_pct": str(change_pct) if change_pct is not None else None,
    }
