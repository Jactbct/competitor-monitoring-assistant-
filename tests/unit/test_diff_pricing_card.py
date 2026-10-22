"""diff / 抖动合并 / 毛利测算 / 8 字段卡片 / 标题词差集。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from domain.diff.diff import diff, fingerprint, merge_jitter
from domain.extract.text import title_gap_terms
from domain.suggest.card import (
    CARD_FIELDS,
    build_a_info_card,
    build_b_promo_card,
    compliance_violations,
)
from domain.suggest.pricing import compute_cost_risk

BASE_FIELDS = {
    "title": "纯棉短袖T恤男夏季宽松五分袖打底衫",
    "price_display": "149.00",
    "price_final": "149.00",
    "promo_signature": "none",
    "main_image": "//img/a.jpg",
    "sku_count": 4,
    "attrs": {"品牌": "简衣", "面料": "棉"},
}


def test_fingerprint_ignores_key_order():
    a = fingerprint({**BASE_FIELDS, "attrs": {"品牌": "简衣", "面料": "棉"}})
    b = fingerprint({**BASE_FIELDS, "attrs": {"面料": "棉", "品牌": "简衣"}})
    assert a == b


def test_fingerprint_changes_on_price():
    assert fingerprint(BASE_FIELDS) != fingerprint({**BASE_FIELDS, "price_final": "114.00"})


def test_diff_reports_price_delta_and_magnitude():
    cur = {**BASE_FIELDS, "price_display": "129.00", "price_final": "114.00", "promo_signature": "店铺券,129,15"}
    result = diff(BASE_FIELDS, cur)
    assert "price_final" in result.changed_fields
    assert set(result.event_types) >= {"real_price", "coupon_promo"}
    assert result.deltas["price_final"]["pct"] == "-0.2349"
    assert result.magnitude == Decimal("0.2349")
    assert result.significant is True


def test_diff_ignores_noise_only_changes():
    cur = {**BASE_FIELDS, "attrs": {"品牌": "简衣"}}
    result = diff(BASE_FIELDS, cur)
    assert result.changed_fields == ["attrs"]
    assert result.significant is False


def test_diff_no_change():
    assert diff(BASE_FIELDS, dict(BASE_FIELDS)).is_empty


def test_jitter_merge_folds_oscillation():
    t0 = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    events = [
        (t0, Decimal("129.00")),
        (t0 + timedelta(minutes=5), Decimal("119.00")),
        (t0 + timedelta(minutes=12), Decimal("129.00")),
        (t0 + timedelta(minutes=90), Decimal("109.00")),
    ]
    merged = merge_jitter(events)
    assert len(merged) == 2
    assert merged[0][2] == 3
    # 净变化为 0（129→129），但仍保留窗口内最大波动 7.75%，避免「合并后看不出抖过」
    assert merged[0][1] == Decimal("0.0775")


def test_margin_and_breakeven():
    risk = compute_cost_risk(Decimal("139.00"), Decimal("78.00"), Decimal("114.00"), Decimal("0.22"))
    assert risk.margin_before == Decimal("0.439")
    assert risk.margin_after == Decimal("0.316")
    assert risk.breakeven_volume_ratio == Decimal("1.39")
    assert risk.unit_concession == Decimal("25.00")
    assert risk.below_floor is False


def test_margin_floor_breach_flagged():
    risk = compute_cost_risk(Decimal("299.00"), Decimal("196.00"), Decimal("229.00"), Decimal("0.22"))
    assert risk.margin_after == Decimal("0.144")
    assert risk.below_floor is True
    assert "低于底线" in risk.note


def test_missing_cost_blocks_measurement():
    risk = compute_cost_risk(Decimal("139.00"), None, Decimal("114.00"))
    assert risk.cost_missing is True
    assert risk.breakeven_volume_ratio is None


def test_card_has_all_eight_fields():
    card = build_a_info_card(
        now=datetime(2026, 10, 21, 10, 0, tzinfo=UTC),
        priority="P1",
        our_title="纯棉短袖T恤男 夏季 宽松",
        comp_title="纯棉短袖T恤男夏季宽松五分袖打底衫",
        our_attrs={"品牌": "简衣"},
        comp_attrs={"品牌": "简衣", "面料": "棉"},
        evidence=[{"snapshot_evidence_id": "snap-1", "quote": "引用", "captured_at": "2026-10-21T10:00:00"}],
    )
    assert card.validate() == []
    payload = card.to_dict()
    assert all(field in payload and payload[field] for field in CARD_FIELDS)


def test_banned_copy_is_flagged():
    hits = compliance_violations("这是全网最低价，绝对第一品牌", "永久有效")
    assert "最低" in "、".join(hits) or hits


def test_title_gap_terms_are_usable_words_not_fragments():
    gaps = title_gap_terms("纯棉短袖T恤男 夏季 宽松", "纯棉短袖T恤男夏季宽松五分袖打底衫")
    assert "五分袖" in gaps
    assert "打底衫" in gaps
    assert "分袖" not in gaps
    assert "季宽" not in gaps


def test_b_promo_card_degrades_wording_when_price_uncertain():
    card = build_b_promo_card(
        now=datetime(2026, 10, 21, 10, 0, tzinfo=UTC),
        priority="P1",
        comp_title="竞品T恤",
        comp_price_final=None,
        comp_price_display=Decimal("129.00"),
        our_price=Decimal("139.00"),
        our_cost=Decimal("78.00"),
        margin_floor=Decimal("0.22"),
        evidence=[{"snapshot_evidence_id": "snap-1", "quote": "券后价", "captured_at": "2026-10-21T10:00:00"}],
        price_uncertain=True,
    )
    assert "展示价" in card.conclusion
    assert "到手价" not in card.conclusion
    assert card.validate() == []
