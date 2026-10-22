"""决策层：优先级映射、A/B 类闸门、降级出口。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from domain.analyze.decide import AnalysisContext, decide, resolve_priority, should_analyze

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
EVIDENCE = [{"snapshot_evidence_id": "snap-1", "quote": "满129减15", "captured_at": "2026-10-21T10:00:00"}]


def ctx(**overrides) -> AnalysisContext:
    base = dict(
        now=NOW,
        tenant_priority="core",
        changed_fields=["price_display", "price_final", "promo_signature"],
        event_types=["real_price", "coupon_promo"],
        deltas={
            "price_display": {"before": "149.00", "after": "129.00", "pct": "-0.1342"},
            "price_final": {"before": "149.00", "after": "114.00", "pct": "-0.2349"},
        },
        magnitude=Decimal("0.2349"),
        cur_fields={
            "title": "纯棉短袖T恤男夏季宽松五分袖打底衫",
            "attrs": {"品牌": "简衣", "面料": "棉"},
            "price_final": "114.00",
        },
        prev_fields={"title": "纯棉短袖T恤男夏季宽松五分袖打底衫", "promo_signature": "none"},
        cur_price_final=Decimal("114.00"),
        cur_price_display=Decimal("129.00"),
        price_uncertain=False,
        our_sku="TS-001",
        our_title="纯棉短袖T恤男 夏季 宽松",
        our_attrs={"品牌": "简衣"},
        our_price=Decimal("139.00"),
        our_cost=Decimal("78.00"),
        margin_floor=Decimal("0.22"),
        evidence=EVIDENCE,
        b_class_enabled=False,
    )
    base.update(overrides)
    return AnalysisContext(**base)


def test_priority_core_with_big_drop_is_p0():
    assert resolve_priority("core", ["real_price"], Decimal("0.12")) == "P0"


def test_priority_core_small_change_is_p1():
    assert resolve_priority("core", ["real_price"], Decimal("0.01")) == "P1"


def test_priority_long_tail_never_p0():
    assert resolve_priority("long_tail", ["real_price"], Decimal("0.5")) == "P2"


def test_priority_normal_price_is_p1():
    assert resolve_priority("normal", ["real_price"], Decimal("0.2")) == "P1"
    assert resolve_priority("normal", ["title"], None) == "P2"


def test_b_gate_closed_yields_price_notice_requiring_confirmation():
    """一期主打 A 类：价格变化只给数字，必须人工确认（P-1）。"""
    decision = decide(ctx())
    assert decision.kind == "B_promo"
    assert decision.quality_gate == "manual_required"
    assert decision.priority == "P0"
    assert "114.00" in decision.card["conclusion"]
    assert decision.card["cost_risk"]["breakeven_volume_ratio"] == "1.39"


def test_b_gate_open_with_cost_produces_executable_card():
    decision = decide(ctx(b_class_enabled=True), llm=None)
    assert decision.kind == "B_promo"
    assert decision.quality_gate == "pass"
    assert "券" in decision.card["action"] or "对齐" in decision.card["action"]


def test_b_gate_open_without_cost_guides_to_fill_cost():
    decision = decide(ctx(b_class_enabled=True, our_cost=None))
    assert "成本" in decision.card["conclusion"] or "成本" in decision.card["action"]
    assert decision.card["cost_risk"].get("cost_missing") in (True, None)


def test_info_only_change_is_a_class():
    decision = decide(
        ctx(
            changed_fields=["title"],
            event_types=["title"],
            deltas={"title": {"before": "短标题", "after": "长标题"}},
            magnitude=None,
        )
    )
    assert decision.kind == "A_info"
    assert decision.quality_gate == "pass"


def test_llm_degraded_falls_back_to_fact_card(fake_llm):
    from domain.analyze.prompt_loader import load_prompts

    llm = fake_llm({"attribution": "瞎写", "evidence_refs": []})
    decision = decide(ctx(), llm=llm, prompts=load_prompts())
    assert decision.degraded is True
    assert decision.card["conclusion"].startswith("竞品「")
    assert "人工核对" in decision.card["action"]


def test_uncertain_price_uses_display_price_wording():
    decision = decide(ctx(price_uncertain=True, cur_price_final=None, b_class_enabled=True))
    assert "展示价" in decision.card["conclusion"]
    assert "到手价" not in decision.card["conclusion"]


def test_cost_floor_breach_blocks_follow_price():
    decision = decide(
        ctx(
            b_class_enabled=True,
            our_price=Decimal("299.00"),
            our_cost=Decimal("196.00"),
            cur_price_final=Decimal("229.00"),
            cur_price_display=Decimal("259.00"),
        )
    )
    assert decision.card["cost_risk"]["below_floor"] is True
    assert "不建议直接跟价" in decision.card["action"] and "低于底线" in decision.card["action"]


def test_no_change_skips_llm():
    assert should_analyze([], False) is False
    assert should_analyze(["price_final"], False) is False
    assert should_analyze(["price_final"], True) is True
