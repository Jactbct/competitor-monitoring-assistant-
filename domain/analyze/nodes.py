"""N1 / N2 节点：schema 校验 + 证据子串校验 + 规则回算 + 降级。

铁律：**LLM 是流水线里的函数，不是调度者**。它的输出必须先过这三道门才可能进卡片。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import ValidationError

from domain.analyze.ports import LlmPort, LlmResult
from domain.analyze.schemas import AnalysisOutput, PromoExtraction
from domain.extract.promo import PromoItem
from domain.extract.text import normalize

__all__ = [
    "N1Result",
    "N2Result",
    "evidence_substring_ok",
    "n1_extract_promos",
    "n2_analyze",
    "reconcile_with_rules",
]

N1_CONFIDENCE_FLOOR = 0.6
N2_CONFIDENCE_FLOOR = 0.65


@dataclass
class N1Result:
    items: list[PromoItem] = field(default_factory=list)
    degraded: bool = False
    dropped: int = 0
    conflicts: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    noise_detected: bool = False


@dataclass
class N2Result:
    output: AnalysisOutput | None = None
    degraded: bool = False
    reason: str = ""
    meta: dict = field(default_factory=dict)


def evidence_substring_ok(quote: str, page_text: str) -> bool:
    """证据必须是原文归一化后的子串 —— 整个防幻觉体系里最有效的一条。"""
    needle = _squash(quote)
    if not needle:
        return False
    return needle in _squash(page_text)


def _squash(text: str) -> str:
    """去掉所有空白再比对：模型经常把「满129减15」写成「满 129 减 15」。"""
    return re.sub(r"\s+", "", normalize(text))


def _to_promo_item(item) -> PromoItem:
    return PromoItem(
        activity_type=item.activity_type,
        threshold_yuan=item.threshold_yuan,
        discount_yuan=item.discount_yuan,
        repeat_tiers=item.repeat_tiers,
        evidence_text=normalize(item.evidence_text),
        confidence=item.confidence,
        source="llm",
    )


def reconcile_with_rules(llm_items: list[PromoItem], rule_items: list[PromoItem]) -> tuple[list[PromoItem], list[str]]:
    """数字以规则为准：同类型同门槛的条目，若面额不一致，取规则值并记冲突。"""
    merged: list[PromoItem] = []
    conflicts: list[str] = []
    rule_index = {(i.activity_type, str(i.threshold_yuan)): i for i in rule_items}

    for item in llm_items:
        key = (item.activity_type, str(item.threshold_yuan))
        rule = rule_index.get(key)
        if (
            rule is not None
            and rule.discount_yuan is not None
            and item.discount_yuan is not None
            and rule.discount_yuan != item.discount_yuan
        ):
            conflicts.append(
                f"{item.activity_type} 面额不一致：模型 {item.discount_yuan} / 规则 {rule.discount_yuan}，取规则值"
            )
            merged.append(rule)
            continue
        merged.append(item)

    # 规则命中但模型漏掉的条目补回来（漏抽比错抽危险）
    llm_keys = {(i.activity_type, str(i.threshold_yuan)) for i in merged}
    for rule in rule_items:
        if (rule.activity_type, str(rule.threshold_yuan)) not in llm_keys:
            merged.append(rule)
    return merged, conflicts


def n1_extract_promos(
    llm: LlmPort | None,
    *,
    page_text: str,
    rule_items: list[PromoItem],
    system_prompt: str,
    user_prompt: str,
    model: str,
    cache_material: str,
    tenant_id: str | None = None,
) -> N1Result:
    """促销抽取节点。无 LLM 时直接返回规则结果（这不是降级，是确定性路径）。"""
    if llm is None:
        return N1Result(items=list(rule_items), meta={"model": "rules", "cached": False}, degraded=False)

    try:
        result: LlmResult = llm.complete_json(
            node="n1",
            system=system_prompt,
            user=user_prompt,
            model=model,
            cache_material=cache_material,
            tenant_id=tenant_id,
        )
        parsed = PromoExtraction.model_validate(result.data)
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        return N1Result(
            items=list(rule_items),
            degraded=True,
            meta={"model": model, "error": type(exc).__name__},
        )

    kept: list[PromoItem] = []
    dropped = 0
    for item in parsed.items:
        if item.confidence < N1_CONFIDENCE_FLOOR:
            dropped += 1
            continue
        if not evidence_substring_ok(item.evidence_text, page_text):
            dropped += 1
            continue
        kept.append(_to_promo_item(item))

    if not kept:
        # 全部被证据校验丢掉 → 整个节点降级，只用规则结果
        return N1Result(
            items=list(rule_items),
            degraded=True,
            dropped=dropped,
            meta=result.meta(),
            noise_detected=parsed.page_noise_detected,
        )

    merged, conflicts = reconcile_with_rules(kept, rule_items)
    return N1Result(
        items=merged,
        degraded=False,
        dropped=dropped,
        conflicts=conflicts,
        meta=result.meta(),
        noise_detected=parsed.page_noise_detected,
    )


def n2_analyze(
    llm: LlmPort | None,
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    cache_material: str,
    valid_evidence_ids: set[str],
    tenant_id: str | None = None,
) -> N2Result:
    """归因 + 影响 + 建议（单次调用）。任何门不过都不重试第 3 次，直接降级。"""
    if llm is None:
        return N2Result(degraded=True, reason="未启用 LLM，使用规则归因", meta={"model": "rules"})

    for attempt in range(2):
        try:
            result: LlmResult = llm.complete_json(
                node="n2",
                system=system_prompt,
                user=user_prompt,
                model=model,
                cache_material=f"{cache_material}:a{attempt}",
                tenant_id=tenant_id,
            )
            parsed = AnalysisOutput.model_validate(result.data)
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            if attempt == 0:
                continue
            return N2Result(degraded=True, reason=f"schema 两次不合法：{type(exc).__name__}", meta={})

        unknown = [ref for ref in parsed.evidence_refs if ref not in valid_evidence_ids]
        if unknown:
            if attempt == 0:
                continue
            return N2Result(degraded=True, reason=f"证据 ID 无法回链：{unknown[:3]}", meta=result.meta())

        if parsed.attribution_confidence < N2_CONFIDENCE_FLOOR:
            return N2Result(output=parsed, degraded=True, reason="归因置信度不足", meta=result.meta())

        return N2Result(output=parsed, degraded=False, meta=result.meta())

    return N2Result(degraded=True, reason="未知失败", meta={})


def decimal_or_none(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
