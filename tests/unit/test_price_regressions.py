"""到手价求解的回归用例（每条对应一次真实审查发现）。"""

from __future__ import annotations

from decimal import Decimal

from domain.extract.price import CONFIDENCE_UNCERTAIN, compute_final_price
from domain.extract.promo import TYPE_PLATFORM_COUPON, TYPE_SHOP_COUPON, PromoItem


def test_coupon_threshold_uses_current_payable():
    """平台券扣完后，店铺券必须按**新的**应付金额判门槛。

    旧实现把 payable 算在循环外：120 元商品先用平台券减 20（应付 100），
    店铺券满 110 减 10 本该不生效，却因为 still 用旧值 120 而被放行 → 系统性低估到手价。
    """
    items = [
        PromoItem(TYPE_PLATFORM_COUPON, None, Decimal("20")),
        PromoItem(TYPE_SHOP_COUPON, Decimal("110"), Decimal("10")),
    ]
    result = compute_final_price(Decimal("120.00"), items)

    assert result.final == Decimal("100.00")  # 只减平台券
    applied = [line for line in result.lines if line.applied]
    assert len(applied) == 1
    assert applied[0].activity_type == TYPE_PLATFORM_COUPON


def test_coupon_threshold_still_applies_when_met():
    items = [
        PromoItem(TYPE_PLATFORM_COUPON, None, Decimal("10")),
        PromoItem(TYPE_SHOP_COUPON, Decimal("100"), Decimal("10")),
    ]
    result = compute_final_price(Decimal("120.00"), items)
    assert result.final == Decimal("100.00")


def test_zero_display_price_is_treated_as_missing():
    """0 元更可能是没抓到价；不能夹到 0.01 假装算出来了。"""
    result = compute_final_price(Decimal("0"), [])
    assert result.final is None
    assert result.confidence == CONFIDENCE_UNCERTAIN


def test_each_full_reduce_appears_in_lines_when_conflicting():
    """满减与每满减并存时，每满减也要在明细里（标未参与计算），不能凭空消失。"""
    from domain.extract.promo import TYPE_EACH_FULL_REDUCE, TYPE_FULL_REDUCE

    items = [
        PromoItem(TYPE_FULL_REDUCE, Decimal("200"), Decimal("30")),
        PromoItem(TYPE_EACH_FULL_REDUCE, Decimal("200"), Decimal("30"), repeat_tiers=-1),
    ]
    result = compute_final_price(Decimal("500.00"), items)
    labels = " ".join(line.label for line in result.lines)
    assert "每满" in labels
    assert result.confidence == CONFIDENCE_UNCERTAIN
