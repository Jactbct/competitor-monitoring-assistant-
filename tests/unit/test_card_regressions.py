"""建议卡回归：促销-only 事件不能让分析任务崩，成本异常不能给出负数让利。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from domain.analyze.decide import AnalysisContext, decide


def _ctx(**overrides) -> AnalysisContext:
    base = dict(
        now=datetime(2026, 10, 22, 10, 0, tzinfo=UTC),
        tenant_priority="core",
        changed_fields=["promo_signature"],
        event_types=["coupon_promo"],
        # 关键：只有促销变化，**没有** price_final / price_display 的 delta
        deltas={"promo_signature": {"before": "none", "after": "店铺券,129,15"}},
        magnitude=Decimal("0.03"),
        cur_fields={"title": "纯棉短袖T恤男夏季宽松", "attrs": {}},
        prev_fields={"promo_signature": "none"},
        cur_price_final=Decimal("129.00"),
        cur_price_display=Decimal("129.00"),
        price_uncertain=False,
        our_sku="TS-001",
        our_title="纯棉短袖T恤男 夏季",
        our_attrs={},
        our_price=Decimal("139.00"),
        our_cost=Decimal("78.00"),
        margin_floor=Decimal("0.22"),
        evidence=[{"snapshot_evidence_id": "snap-1", "quote": "满129减15", "captured_at": "2026-10-22T10:00:00"}],
        b_class_enabled=False,
    )
    base.update(overrides)
    return AnalysisContext(**base)


def test_promo_only_event_does_not_crash():
    """B1：price_delta 为 None 时旧实现会抛 AttributeError，把任务打进死信队列。"""
    decision = decide(_ctx())
    assert decision.card["conclusion"]
    assert "129.00" in decision.card["conclusion"]


def test_promo_only_conclusion_has_no_leading_comma():
    """m2：pct 缺失时结论不能以「，」开头。"""
    decision = decide(_ctx())
    assert not decision.card["conclusion"].startswith("，")
    assert decision.card["conclusion"].startswith("竞品")


def test_cost_above_price_gives_no_negative_concession():
    """M14：成本 ≥ 售价时不能给出「单件让利上限 -122 元」这种不可能的结论。"""
    decision = decide(
        _ctx(
            our_price=Decimal("100.00"),
            our_cost=Decimal("150.00"),
            cur_price_final=Decimal("60.00"),
            cur_price_display=Decimal("70.00"),
            event_types=["real_price"],
            deltas={"price_final": {"before": "90.00", "after": "60.00", "pct": "-0.3333"}},
            b_class_enabled=True,
        )
    )
    assert "成本数据异常" in decision.card["action"]
    assert "-" not in decision.card["action"].replace("成本数据异常（成本已吃掉全部毛利），先核对成本与售价再谈跟价", "")


def test_price_notice_with_pct_missing_still_reads_well():
    decision = decide(
        _ctx(
            event_types=["real_price"],
            changed_fields=["price_final"],
            deltas={"price_final": {"before": "0.00", "after": "99.00"}},
            cur_price_final=Decimal("99.00"),
            cur_price_display=Decimal("99.00"),
        )
    )
    assert decision.card["conclusion"].startswith("竞品")
    assert "现价 99.00 元" in decision.card["conclusion"]
