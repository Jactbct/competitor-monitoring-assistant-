"""到手价求解器：这是出过 bug 的地方（原型 92.5% → 100%），必须逐个规则钉住。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.extract.price import CONFIDENCE_CERTAIN, CONFIDENCE_UNCERTAIN, compute_final_price
from domain.extract.promo import (
    TYPE_EACH_FULL_REDUCE,
    TYPE_FULL_REDUCE,
    TYPE_INSTANT,
    TYPE_SHOP_COUPON,
    PromoItem,
    parse_promos,
)


def full(threshold: str, discount: str) -> PromoItem:
    return PromoItem(TYPE_FULL_REDUCE, Decimal(threshold), Decimal(discount), evidence_text="满X减Y")


def test_full_reduce_not_reached_is_certain():
    """门槛未达到是确定性结论 —— 不能标不确定，否则 B 类建议全废。"""
    result = compute_final_price(Decimal("89.00"), [full("199", "20"), PromoItem(TYPE_INSTANT, None, Decimal("10"))])
    assert result.final == Decimal("79.00")
    assert result.confidence == CONFIDENCE_CERTAIN
    assert result.reasons == []


def test_each_full_reduce_counts_tiers():
    item = PromoItem(TYPE_EACH_FULL_REDUCE, Decimal("200"), Decimal("30"), repeat_tiers=-1)
    result = compute_final_price(Decimal("659.00"), [item])
    assert result.final == Decimal("569.00")  # 659 - 30*3
    assert result.confidence == CONFIDENCE_CERTAIN


def test_each_full_reduce_not_parsed_as_full_reduce():
    """「每满200减30」不能被「满200减30」再命中一次，否则会误判成规则冲突。"""
    parsed = parse_promos(["每满200减30，上不封顶"])
    types = [i.activity_type for i in parsed.items]
    assert types == [TYPE_EACH_FULL_REDUCE]
    assert parsed.conflicts == []


def test_shop_coupon_with_threshold_not_applied_below_threshold():
    coupon = PromoItem(TYPE_SHOP_COUPON, Decimal("199"), Decimal("20"), evidence_text="店铺券满199减20")
    result = compute_final_price(Decimal("120.00"), [coupon])
    assert result.final == Decimal("120.00")
    assert result.confidence == CONFIDENCE_CERTAIN
    assert result.lines[0].applied is False


def test_shop_coupon_applied_when_threshold_met():
    coupon = PromoItem(TYPE_SHOP_COUPON, Decimal("129"), Decimal("15"), evidence_text="满129减15")
    result = compute_final_price(Decimal("129.00"), [coupon])
    assert result.final == Decimal("114.00")
    assert result.confidence == CONFIDENCE_CERTAIN


def test_two_full_reduce_tiers_is_uncertain_but_computes_best():
    result = compute_final_price(Decimal("300.00"), [full("299", "30"), full("199", "20")])
    assert result.final == Decimal("270.00")  # 取最优档
    assert result.confidence == CONFIDENCE_UNCERTAIN
    assert any("满减" in r for r in result.reasons)


def test_full_reduce_and_each_conflict_is_uncertain():
    items = [full("200", "30"), PromoItem(TYPE_EACH_FULL_REDUCE, Decimal("200"), Decimal("30"), repeat_tiers=-1)]
    result = compute_final_price(Decimal("500.00"), items)
    assert result.confidence == CONFIDENCE_UNCERTAIN
    assert any("每满减" in r for r in result.reasons)


def test_multiple_instant_discounts_is_uncertain():
    items = [PromoItem(TYPE_INSTANT, None, Decimal("10")), PromoItem(TYPE_INSTANT, None, Decimal("5"))]
    result = compute_final_price(Decimal("100.00"), items)
    assert result.confidence == CONFIDENCE_UNCERTAIN


def test_missing_display_price_returns_no_estimate():
    result = compute_final_price(None, [full("100", "10")])
    assert result.final is None
    assert result.confidence == CONFIDENCE_UNCERTAIN
    assert result.reasons


def test_never_below_one_cent():
    items = [PromoItem(TYPE_INSTANT, None, Decimal("999"))]
    result = compute_final_price(Decimal("10.00"), items)
    assert result.final == Decimal("0.01")
    assert result.confidence == CONFIDENCE_UNCERTAIN


def test_seckill_replaces_base_price():
    item = PromoItem("秒杀", sekill_price=Decimal("79.00"), evidence_text="秒杀价79")
    result = compute_final_price(Decimal("129.00"), [item])
    assert result.final == Decimal("79.00")
    assert result.confidence == CONFIDENCE_CERTAIN


def test_price_detail_lines_are_serialisable():
    result = compute_final_price(Decimal("129.00"), [PromoItem(TYPE_SHOP_COUPON, Decimal("129"), Decimal("15"))])
    payload = result.to_dict()
    assert payload["final"] == "114.00"
    assert payload["lines"][0]["applied"] is True
    assert isinstance(payload["lines"][0]["amount"], str)


@pytest.mark.parametrize(
    ("text", "expected_type"),
    [
        ("满199减20", TYPE_FULL_REDUCE),
        ("每满200减30", TYPE_EACH_FULL_REDUCE),
        ("店铺券 20 元", TYPE_SHOP_COUPON),
        ("限时立减10", TYPE_INSTANT),
    ],
)
def test_promo_type_classification(text, expected_type):
    parsed = parse_promos([text])
    assert parsed.items, f"未解析出促销：{text}"
    assert parsed.items[0].activity_type == expected_type


def test_coupon_sentence_reclassified_from_full_reduce():
    """「店铺券 满129减15」先被满减模式命中，需按语境纠正为券。"""
    parsed = parse_promos(["满129减15 店铺券，每人限领 1 张"])
    assert parsed.items[0].activity_type == TYPE_SHOP_COUPON
    assert parsed.items[0].threshold_yuan == Decimal("129")
