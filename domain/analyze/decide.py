"""决策层：把「客观变化 + 租户配置 + 我方商品」翻译成一条建议或一张事实卡。

这里体现两个产品判断：

* **一期主打 A 类（P-1 决策）**：``b_class_enabled`` 默认关闭。到手价可确定率未过
  M1 闸门（≥70%）前，B 类跟价只做「展示价对比 + 请人工核算」，不给调价数字。
* **降级不是失败**：任何一环不确定，就退到「只报事实」，并在 `degraded` 上留痕，
  按周统计降级率 —— 这是最有价值的运维指标之一。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from domain.analyze.nodes import n2_analyze
from domain.analyze.ports import LlmPort
from domain.analyze.prompt_loader import PromptBundle
from domain.analyze.schemas import AnalysisOutput
from domain.extract.promo import PromoItem
from domain.extract.text import title_gap_terms
from domain.suggest.card import (
    Card,
    build_a_info_card,
    build_b_promo_card,
    build_cost_missing_card,
    build_fact_card,
    build_price_notice_card,
)

__all__ = ["AnalysisContext", "Decision", "decide", "resolve_priority", "should_analyze"]

_PRICE_EVENTS = {"real_price", "coupon_promo"}
_INFO_FIELDS = {"title", "main_image", "sku_count", "attrs"}


@dataclass
class AnalysisContext:
    now: datetime
    tenant_priority: str = "normal"
    changed_fields: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    deltas: dict = field(default_factory=dict)
    magnitude: Decimal | None = None
    cur_fields: dict = field(default_factory=dict)
    prev_fields: dict = field(default_factory=dict)
    cur_price_final: Decimal | None = None
    cur_price_display: Decimal | None = None
    price_uncertain: bool = False
    our_sku: str = ""
    our_title: str = ""
    our_attrs: dict = field(default_factory=dict)
    our_price: Decimal | None = None
    our_cost: Decimal | None = None
    margin_floor: Decimal = Decimal("0.22")
    evidence: list[dict] = field(default_factory=list)
    page_text: str = ""
    promo_items: list[PromoItem] = field(default_factory=list)
    b_class_enabled: bool = False
    cache_material: str = ""
    tenant_id: str | None = None


@dataclass
class Decision:
    kind: str
    priority: str
    card: dict
    attribution: str
    threat_level: str
    confidence: Decimal
    degraded: bool
    degraded_reason: str
    quality_gate: str
    violations: list[str]
    model_meta: dict
    threat_reason: str = ""

    @property
    def is_pushable(self) -> bool:
        return self.quality_gate == "pass" and not self.degraded


def should_analyze(changed_fields: list[str], significant: bool) -> bool:
    """没有实质变化就不进 LLM（变化率约 8%，调用量因此降 92%）。"""
    return bool(changed_fields) and significant


def resolve_priority(tenant_priority: str, event_types: list[str], magnitude: Decimal | None) -> str:
    price_or_promo = any(t in _PRICE_EVENTS for t in event_types)
    mag = magnitude or Decimal("0")
    if tenant_priority == "core":
        if price_or_promo and mag >= Decimal("0.03"):
            return "P0"
        return "P1"
    if tenant_priority == "normal":
        return "P1" if price_or_promo else "P2"
    return "P2"


def _rule_attribution(ctx: AnalysisContext) -> tuple[str, Decimal]:
    """没有 LLM 时的确定性归因。按「证据强度」从强到弱判定。"""
    prev_promos = ctx.prev_fields.get("promo_signature") or "none"
    cur_promos = ctx.cur_fields.get("promo_signature") or "none"
    price_changed = "price_final" in ctx.deltas or "price_display" in ctx.deltas
    promo_changed = prev_promos != cur_promos

    if promo_changed and cur_promos == "none":
        return "活动结束", Decimal("0.80")
    if promo_changed and price_changed:
        return "活动开始", Decimal("0.78")
    if promo_changed:
        return "券力度变化", Decimal("0.75")
    if "sku_count" in ctx.changed_fields:
        return "SKU结构变更", Decimal("0.70")
    if price_changed:
        return "真实调价", Decimal("0.85")
    return "页面改版", Decimal("0.60")


def _rule_threat(ctx: AnalysisContext) -> tuple[str, str]:
    price = ctx.cur_price_final if not ctx.price_uncertain else ctx.cur_price_display
    if price is None or ctx.our_price is None or ctx.our_price <= 0:
        return "low", "无法比较双方价格"
    gap = (ctx.our_price - price) / ctx.our_price
    if gap >= Decimal("0.05"):
        return "high", f"竞品价格低于我方 {gap:.1%}"
    if gap >= Decimal("0.01"):
        return "medium", f"竞品价格低于我方 {gap:.1%}"
    return "low", "竞品价格高于或持平我方"


def _info_gap(ctx: AnalysisContext) -> bool:
    comp_title = str(ctx.cur_fields.get("title") or "")
    missing = title_gap_terms(ctx.our_title, comp_title)
    comp_attrs = ctx.cur_fields.get("attrs") or {}
    missing_attrs = [k for k in comp_attrs if k not in (ctx.our_attrs or {})] if isinstance(comp_attrs, dict) else []
    return bool(missing or missing_attrs)


def _classify(ctx: AnalysisContext) -> str:
    """选卡型。**一期主打 A 类**：价格类默认落 ``price_notice``（给数字、要人确认）。"""
    has_price = any(t in _PRICE_EVENTS for t in ctx.event_types)
    if has_price:
        if ctx.b_class_enabled and ctx.our_cost is None:
            return "B_promo_cost_missing"
        if (
            ctx.b_class_enabled
            and not ctx.price_uncertain
            and ctx.our_cost is not None
            and ctx.our_price is not None
        ):
            return "B_promo"
        return "price_notice"
    if _info_gap(ctx):
        return "A_info"
    return "fact"


def _build_card(ctx: AnalysisContext, kind: str, priority: str) -> Card:
    if kind == "B_promo":
        card = build_b_promo_card(
            now=ctx.now,
            priority=priority,
            comp_title=str(ctx.cur_fields.get("title") or ""),
            comp_price_final=ctx.cur_price_final,
            comp_price_display=ctx.cur_price_display,
            our_price=ctx.our_price,
            our_cost=ctx.our_cost,
            margin_floor=ctx.margin_floor,
            evidence=ctx.evidence,
            price_uncertain=ctx.price_uncertain,
        )
        return card
    if kind == "B_promo_cost_missing":
        return build_cost_missing_card(
            now=ctx.now, priority=priority, product_sku=ctx.our_sku or "未命名 SKU", evidence=ctx.evidence
        )
    if kind == "price_notice":
        return build_price_notice_card(
            now=ctx.now,
            priority=priority,
            comp_title=str(ctx.cur_fields.get("title") or ""),
            comp_price_display=ctx.cur_price_display,
            comp_price_final=ctx.cur_price_final,
            price_uncertain=ctx.price_uncertain,
            our_price=ctx.our_price,
            our_cost=ctx.our_cost,
            margin_floor=ctx.margin_floor,
            evidence=ctx.evidence,
            price_delta=ctx.deltas.get("price_final") or ctx.deltas.get("price_display"),
        )
    if kind == "A_info":
        return build_a_info_card(
            now=ctx.now,
            priority=priority,
            our_title=ctx.our_title,
            comp_title=str(ctx.cur_fields.get("title") or ""),
            our_attrs=ctx.our_attrs or {},
            comp_attrs=ctx.cur_fields.get("attrs") or {},
            evidence=ctx.evidence,
            comp_price_final=ctx.cur_price_final,
        )
    return build_fact_card(
        now=ctx.now,
        priority=priority,
        target_title=str(ctx.cur_fields.get("title") or "竞品商品"),
        changed_fields=ctx.changed_fields,
        deltas=ctx.deltas,
        evidence=ctx.evidence,
    )


def _build_user_prompt(ctx: AnalysisContext) -> str:
    lines = [
        "【变化事实】",
        f"变化字段：{', '.join(ctx.changed_fields)}",
        f"事件类型：{', '.join(ctx.event_types)}",
        f"前后值：{ctx.deltas}",
        f"变化幅度：{ctx.magnitude}",
        f"当前到手价：{ctx.cur_price_final}（价格置信度：{'不确定' if ctx.price_uncertain else '确定'}）",
        f"当前展示价：{ctx.cur_price_display}",
        "",
        "【我方商品】",
        f"SKU：{ctx.our_sku or '未绑定'}",
        f"标题：{ctx.our_title or '未维护'}",
        f"售价：{ctx.our_price}",
        f"成本：{ctx.our_cost if ctx.our_cost is not None else '缺失'}",
        f"毛利底线：{ctx.margin_floor}",
        "",
        "【可用证据】",
    ]
    for item in ctx.evidence:
        lines.append(f"- {item.get('snapshot_evidence_id')}: {item.get('quote')}")
    lines.append("")
    lines.append("请按要求输出 JSON。")
    return "\n".join(lines)


def decide(ctx: AnalysisContext, llm: LlmPort | None = None, prompts: PromptBundle | None = None) -> Decision:
    attribution, confidence = _rule_attribution(ctx)
    threat_level, threat_reason = _rule_threat(ctx)
    model_meta: dict = {"model": "rules", "prompt_version": prompts.version if prompts else "rules"}
    degraded = False
    degraded_reason = ""

    if llm is not None and prompts is not None:
        valid_ids = {str(e.get("snapshot_evidence_id")) for e in ctx.evidence if e.get("snapshot_evidence_id")}
        n2 = n2_analyze(
            llm,
            system_prompt=prompts.n2_system,
            user_prompt=_build_user_prompt(ctx),
            model="reason",
            cache_material=ctx.cache_material,
            valid_evidence_ids=valid_ids,
            tenant_id=ctx.tenant_id,
        )
        model_meta = {**model_meta, **n2.meta}
        output: AnalysisOutput | None = n2.output
        if output is not None:
            attribution = output.attribution
            confidence = Decimal(str(output.attribution_confidence)).quantize(Decimal("0.001"))
            threat_level = output.threat_level
            threat_reason = output.threat_reason
        degraded = n2.degraded
        degraded_reason = n2.reason

    priority = resolve_priority(ctx.tenant_priority, ctx.event_types, ctx.magnitude)
    kind = _classify(ctx)
    card = _build_card(ctx, kind, priority)

    # 归因不可信时不许给结论性建议，只报事实（FR-32）
    if degraded and kind != "fact":
        card = build_fact_card(
            now=ctx.now,
            priority=priority,
            target_title=str(ctx.cur_fields.get("title") or "竞品商品"),
            changed_fields=ctx.changed_fields,
            deltas=ctx.deltas,
            evidence=ctx.evidence,
            reason=degraded_reason,
        )
        kind = "fact"

    problems = card.validate()
    violations = list(card.violations)
    quality_gate = "pass" if not problems and not violations else "manual_required"
    # 价格类在没有打开 B 类闸门时，一律要求人工确认后才推送（P-1）
    if kind == "price_notice":
        quality_gate = "manual_required"

    return Decision(
        kind={"price_notice": "B_promo", "fact": "A_info"}.get(kind, kind),
        priority=priority,
        card=card.to_dict(),
        attribution=attribution,
        threat_level=threat_level,
        confidence=confidence,
        degraded=degraded,
        degraded_reason=degraded_reason,
        quality_gate=quality_gate,
        violations=violations,
        model_meta={**model_meta, "card_problems": problems},
        threat_reason=threat_reason,
    )
