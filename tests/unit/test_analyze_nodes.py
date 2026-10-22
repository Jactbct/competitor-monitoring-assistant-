"""LLM 节点的三道门：证据子串、证据回链、数字以规则为准；以及降级链。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.analyze.nodes import (
    evidence_substring_ok,
    n1_extract_promos,
    n2_analyze,
    reconcile_with_rules,
)
from domain.extract.promo import TYPE_SHOP_COUPON, PromoItem

PAGE = "本店活动：满129减15 店铺券，每人限领 1 张。到手价 114 元。"


def _rule_item(discount: str = "15") -> PromoItem:
    return PromoItem(
        TYPE_SHOP_COUPON,
        Decimal("129"),
        Decimal(discount),
        evidence_text="满129减15 店铺券",
    )


def test_evidence_substring_tolerates_whitespace_and_fullwidth():
    assert evidence_substring_ok("满 129 减 15", PAGE)
    assert evidence_substring_ok("满１２９减１５", PAGE)
    assert not evidence_substring_ok("满199减50", PAGE)


def test_n1_drops_fabricated_evidence(fake_llm):
    llm = fake_llm(
        {
            "items": [
                {
                    "activity_type": "满减",
                    "threshold_yuan": "199",
                    "discount_yuan": "50",
                    "evidence_text": "满199减50",  # 原文里没有 → 必须丢掉
                    "confidence": 0.95,
                }
            ],
            "page_noise_detected": False,
        }
    )
    result = n1_extract_promos(
        llm, page_text=PAGE, rule_items=[_rule_item()], system_prompt="s", user_prompt="u",
        model="small", cache_material="m",
    )
    assert result.dropped == 1
    assert result.degraded is True
    assert [i.activity_type for i in result.items] == [TYPE_SHOP_COUPON]


def test_n1_conflict_takes_rule_value(fake_llm):
    llm = fake_llm(
        {
            "items": [
                {
                    "activity_type": "店铺券",
                    "threshold_yuan": "129",
                    "discount_yuan": "50",  # 模型说减 50，规则说减 15
                    "evidence_text": "满129减15 店铺券",
                    "confidence": 0.9,
                }
            ],
            "page_noise_detected": False,
        }
    )
    result = n1_extract_promos(
        llm, page_text=PAGE, rule_items=[_rule_item()], system_prompt="s", user_prompt="u",
        model="small", cache_material="m",
    )
    assert result.conflicts
    assert result.items[0].discount_yuan == Decimal("15")


def test_reconcile_adds_rules_missed_by_model():
    merged, conflicts = reconcile_with_rules([], [_rule_item()])
    assert len(merged) == 1
    assert conflicts == []


def test_n1_without_llm_is_deterministic_path():
    result = n1_extract_promos(
        None, page_text=PAGE, rule_items=[_rule_item()], system_prompt="", user_prompt="",
        model="", cache_material="",
    )
    assert result.degraded is False
    assert result.meta["model"] == "rules"


def test_n1_invalid_schema_degrades(fake_llm):
    llm = fake_llm({"items": "not-a-list"})
    result = n1_extract_promos(
        llm, page_text=PAGE, rule_items=[_rule_item()], system_prompt="s", user_prompt="u",
        model="small", cache_material="m",
    )
    assert result.degraded is True
    assert result.items  # 退回规则结果，不是空手而归


OK_OUTPUT = {
    "attribution": "活动开始",
    "attribution_confidence": 0.82,
    "threat_level": "high",
    "threat_reason": "竞品到手价低于我方 8%",
    "kind": "B_promo",
    "evidence_refs": ["snap-1"],
}


def test_n2_accepts_valid_output(fake_llm):
    result = n2_analyze(
        fake_llm(OK_OUTPUT), system_prompt="s", user_prompt="u", model="m",
        cache_material="c", valid_evidence_ids={"snap-1"},
    )
    assert result.degraded is False
    assert result.output.attribution == "活动开始"


def test_n2_rejects_unknown_evidence_twice_then_degrades(fake_llm):
    bad = {**OK_OUTPUT, "evidence_refs": ["snap-999"]}
    result = n2_analyze(
        fake_llm(bad), system_prompt="s", user_prompt="u", model="m",
        cache_material="c", valid_evidence_ids={"snap-1"},
    )
    assert result.degraded is True
    assert "证据" in result.reason


def test_n2_retries_once_on_bad_schema_then_succeeds(fake_llm):
    llm = fake_llm([{"attribution": "瞎编的归因"}, OK_OUTPUT])
    result = n2_analyze(
        llm, system_prompt="s", user_prompt="u", model="m", cache_material="c",
        valid_evidence_ids={"snap-1"},
    )
    assert result.degraded is False
    assert llm.calls == 2


def test_n2_low_confidence_marks_degraded(fake_llm):
    low = {**OK_OUTPUT, "attribution_confidence": 0.3}
    result = n2_analyze(
        fake_llm(low), system_prompt="s", user_prompt="u", model="m", cache_material="c",
        valid_evidence_ids={"snap-1"},
    )
    assert result.degraded is True
    assert "置信度" in result.reason


@pytest.mark.parametrize("confidence", [0.59, 0.6])
def test_n1_confidence_floor(fake_llm, confidence):
    llm = fake_llm(
        {
            "items": [
                {
                    "activity_type": "店铺券",
                    "threshold_yuan": "129",
                    "discount_yuan": "15",
                    "evidence_text": "满129减15 店铺券",
                    "confidence": confidence,
                }
            ],
            "page_noise_detected": False,
        }
    )
    result = n1_extract_promos(
        llm,
        page_text=PAGE,
        rule_items=[_rule_item()],
        system_prompt="s",
        user_prompt="u",
        model="small",
        cache_material="m",
    )
    # 低于 0.6 的条目被丢弃，但规则结果仍在（不会因为模型没信心就啥都不给）
    assert result.items
    if confidence < 0.6:
        assert result.items[0].source == "rules"
        assert result.dropped == 1
    else:
        assert result.items[0].source == "llm"
