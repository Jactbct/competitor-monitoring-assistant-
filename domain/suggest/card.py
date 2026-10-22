"""建议卡（8 字段 schema 强制，PRD FR-31 / G2）。

8 字段缺任意一项即视为不合格 → ``quality_gate='manual_required'``，不进自动推送。
卡片里的不合规文案（绝对化用语）直接拦截，不给运营「照抄踩雷」的机会。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from domain.extract.text import title_gap_terms
from domain.suggest.pricing import CostRisk, compute_cost_risk

__all__ = [
    "CARD_FIELDS",
    "build_a_info_card",
    "build_b_promo_card",
    "build_cost_missing_card",
    "build_fact_card",
    "Card",
    "compliance_violations",
]

CARD_FIELDS = (
    "conclusion",
    "evidence",
    "action",
    "owner_role",
    "deadline",
    "expected_impact",
    "cost_risk",
    "acceptance",
)

#: 绝对化/极限用语（广告法风险），命中即拦截
_BANNED_PATTERNS = (
    re.compile(r"(?<![的地得])最(?:好|佳|优|便宜|低价|低|大|高|强|快|新|实惠)"),
    re.compile(r"第一(?:品牌|名|销量)"),
    re.compile(r"国家级|世界级|顶级|绝无仅有|独一无二"),
    re.compile(r"100%|百分百|永久|万能|绝对"),
)


@dataclass
class Card:
    conclusion: str
    evidence: list[dict]
    action: str
    owner_role: str
    deadline: datetime
    expected_impact: str
    cost_risk: dict
    acceptance: str
    kind: str = "A_info"
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "conclusion": self.conclusion,
            "evidence": self.evidence,
            "action": self.action,
            "owner_role": self.owner_role,
            "deadline": self.deadline.isoformat(),
            "expected_impact": self.expected_impact,
            "cost_risk": self.cost_risk,
            "acceptance": self.acceptance,
            "kind": self.kind,
            "violations": self.violations,
        }

    def validate(self) -> list[str]:
        """返回缺失/不合规的字段名列表；空列表 = 通过。"""
        problems: list[str] = []
        for name in CARD_FIELDS:
            value = getattr(self, name, None)
            if value is None or (isinstance(value, str) and not value.strip()):
                problems.append(name)
        if not isinstance(self.evidence, list) or not self.evidence:
            problems.append("evidence")
        if not isinstance(self.cost_risk, dict) or not self.cost_risk:
            problems.append("cost_risk")
        return sorted(set(problems))


def compliance_violations(*texts: str) -> list[str]:
    hits: list[str] = []
    for text in texts:
        if not text:
            continue
        for pattern in _BANNED_PATTERNS:
            for match in pattern.finditer(text):
                hits.append(match.group(0))
    return sorted(set(hits))


def _deadline_for(priority: str, now: datetime) -> datetime:
    if priority == "P0":
        return (now + timedelta(hours=4)).replace(minute=0, second=0, microsecond=0)
    if priority == "P1":
        return (now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    return (now + timedelta(days=3)).replace(hour=12, minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------- #
# B 类：跟价建议
# --------------------------------------------------------------------------- #
def build_b_promo_card(
    *,
    now: datetime,
    priority: str,
    comp_title: str,
    comp_price_final: Decimal | None,
    comp_price_display: Decimal | None,
    our_price: Decimal | None,
    our_cost: Decimal | None,
    margin_floor: Decimal,
    evidence: list[dict],
    price_uncertain: bool,
    attribution: str = "",
) -> Card:
    """B 类卡片。到手价不确定时**降级为展示价对比**，措辞不出现「到手价」。"""
    if price_uncertain or comp_price_final is None:
        basis_price = comp_price_display
        basis_label = "展示价"
        caveat = "（价格待人工核算，以下按展示价对比）"
    else:
        basis_price = comp_price_final
        basis_label = "到手价"
        caveat = ""

    risk: CostRisk = compute_cost_risk(our_price, our_cost, basis_price, margin_floor)

    if our_price is None or basis_price is None:
        conclusion = f"竞品 {comp_title[:20]} 价格信息不足，无法给出跟价结论{caveat}"
    else:
        gap_pct = ((our_price - basis_price) / our_price * 100).quantize(Decimal("0.1")) if our_price else Decimal("0")
        if gap_pct > 0:
            conclusion = (
                f"竞品{basis_label} {basis_price} 元，低于我方 {our_price} 元约 {gap_pct}%，"
                f"建议让利至 {basis_price} 元{caveat}"
            )
        else:
            conclusion = (
                f"竞品{basis_label} {basis_price} 元，高于我方 {our_price} 元，"
                f"暂无跟价必要，可维持现价并观察{caveat}"
            )

    if risk.cost_missing:
        action = "先补录自有商品成本与毛利底线，再由系统生成可执行的跟价方案"
        impact = "补录成本后可自动算出让利额与打平销量"
        acceptance = "成本录入完成后，本建议自动重算并重新推送"
    elif risk.below_floor:
        if risk.margin_before <= risk.margin_floor:
            # 成本 ≥ 售价：任何「让利上限」都是负数，先要求核对成本（不猜）
            action = "成本数据异常（成本已吃掉全部毛利），先核对成本与售价再谈跟价"
            impact = "不评估影响：成本口径不清楚时任何结论都不可信"
        else:
            action = (
                f"不建议直接跟价：跟价后毛利率 {risk.margin_after} 已低于底线 {risk.margin_floor}。"
                f"可选替代：发放小面额店铺券或搭赠，控制单件让利不超过 "
                f"{((risk.margin_before - risk.margin_floor) * (our_price or Decimal('0'))).quantize(Decimal('0.01'))} 元"
            )
            impact = risk.note
        acceptance = f"调整后毛利率不低于 {risk.margin_floor}；7 天后复核转化率变化"
    else:
        action = (
            f"发放店铺券将到手价对齐至 {basis_price} 元，挂牌价维持 {our_price} 元"
            if basis_price
            else "按竞品价格调整促销力度"
        )
        impact = risk.note or "价格对齐后预期恢复转化率"
        acceptance = f"券上线后到手价 = {basis_price} 元；7 天后对比转化率与毛利额"

    card = Card(
        conclusion=conclusion,
        evidence=evidence,
        action=action,
        owner_role="operator",
        deadline=_deadline_for(priority, now),
        expected_impact=impact,
        cost_risk=risk.to_dict(),
        acceptance=acceptance,
        kind="B_promo",
    )
    card.violations = compliance_violations(card.conclusion, card.action, card.expected_impact)
    return card


# --------------------------------------------------------------------------- #
# A 类：商品信息（标题/属性/卖点）—— 一期主打
# --------------------------------------------------------------------------- #
def build_a_info_card(
    *,
    now: datetime,
    priority: str,
    our_title: str,
    comp_title: str,
    our_attrs: dict,
    comp_attrs: dict,
    evidence: list[dict],
    comp_price_final: Decimal | None = None,
) -> Card:
    missing = title_gap_terms(our_title, comp_title)
    missing_attrs = [k for k in comp_attrs if k not in our_attrs][:6]

    if missing:
        term_text = "、".join(missing[:6])
        conclusion = f"竞品标题比我们多覆盖 {len(missing)} 个词：{term_text}"
        action = f"在标题前 30 字内补入「{term_text}」，同时保持主关键词位置不变"
        impact = "标题词覆盖对齐后，预计提升搜索曝光（同类目经验值 5%~15%，需 7 天后核对）"
    elif missing_attrs:
        conclusion = f"标题词覆盖已对齐，但竞品多填了 {len(missing_attrs)} 个属性：{'、'.join(missing_attrs)}"
        action = f"补齐属性「{'、'.join(missing_attrs)}」，填写真实值，不得为空或错填"
        impact = "属性完整度提升有助于筛选场景曝光"
    else:
        conclusion = "标题与属性覆盖均不落后于竞品，无需修改"
        action = "维持现状，继续观察竞品标题变化"
        impact = "无"

    card = Card(
        conclusion=conclusion,
        evidence=evidence,
        action=action,
        owner_role="operator",
        deadline=_deadline_for(priority, now),
        expected_impact=impact,
        cost_risk={"mode": "not_applicable", "reason": "信息类建议不涉及让利", "note": "不涉及价格与毛利"},
        acceptance="改后标题包含上述词且未被平台判定违规；7 天后对比曝光与点击率",
        kind="A_info",
    )
    card.violations = compliance_violations(card.conclusion, card.action, card.expected_impact)
    return card


# --------------------------------------------------------------------------- #
# 降级卡：只报事实（N2 置信度不足 / schema 两次失败）
# --------------------------------------------------------------------------- #
def build_fact_card(
    *,
    now: datetime,
    priority: str,
    target_title: str,
    changed_fields: list[str],
    deltas: dict,
    evidence: list[dict],
    reason: str = "归因置信度不足",
) -> Card:
    parts: list[str] = []
    if "price_final" in deltas:
        entry = deltas["price_final"]
        parts.append(f"到手价 {entry.get('before')} → {entry.get('after')}")
    if "price_display" in deltas:
        entry = deltas["price_display"]
        parts.append(f"展示价 {entry.get('before')} → {entry.get('after')}")
    if "title" in deltas:
        parts.append("标题已变更")
    if "promo_signature" in deltas:
        parts.append("促销规则已变更")
    detail = "；".join(parts) or "、".join(changed_fields)

    card = Card(
        conclusion=f"竞品「{target_title[:24]}」发生变化：{detail}",
        evidence=evidence,
        action="人工核对竞品页面，确认变化性质后再决定是否调整我方策略",
        owner_role="operator",
        deadline=_deadline_for(priority, now),
        expected_impact="未做影响判定（原因待确认），不预估效果",
        cost_risk={"mode": "not_evaluated", "note": "降级模式不做毛利测算"},
        acceptance="人工确认变化性质并记录结论",
        kind="A_info",
    )
    card.violations = compliance_violations(card.conclusion)
    return card


def build_cost_missing_card(
    *, now: datetime, priority: str, product_sku: str, evidence: list[dict]
) -> Card:
    return Card(
        conclusion=f"自有商品 {product_sku} 缺少成本数据，无法生成跟价建议",
        evidence=evidence or [{"snapshot_evidence_id": "n/a", "quote": "自有商品未维护成本", "captured_at": now.isoformat()}],
        action="在「自有商品」页补录成本与毛利底线",
        owner_role="admin",
        deadline=_deadline_for(priority, now),
        expected_impact="补录后系统可自动测算让利与打平销量",
        cost_risk={"cost_missing": True, "mode": "not_evaluable"},
        acceptance="成本录入后 24 小时内收到重算后的跟价建议",
        kind="B_promo",
    )


# --------------------------------------------------------------------------- #
# 价格提示卡：B 类闸门未过时的默认出口（P-1）
# --------------------------------------------------------------------------- #
def build_price_notice_card(
    *,
    now: datetime,
    priority: str,
    comp_title: str,
    comp_price_display: Decimal | None,
    comp_price_final: Decimal | None,
    price_uncertain: bool,
    our_price: Decimal | None,
    our_cost: Decimal | None,
    margin_floor: Decimal,
    evidence: list[dict],
    price_delta: dict | None = None,
    cost_missing: bool = False,
) -> Card:
    """价格变化提示：**给数字但不自动给调价指令**。

    这是 P-1 决策的落地点 —— 一期主打 A 类，B 类（跟价）默认不推送，
    但「竞品降了 23%」这件事必须让运营知道，并且把让利额与打平销量算出来，
    最后由人决定跟不跟。到手价不确定时只用展示价，措辞里不出现「到手价」。
    """
    basis = comp_price_final if (comp_price_final is not None and not price_uncertain) else comp_price_display
    basis_label = "到手价" if (comp_price_final is not None and not price_uncertain) else "展示价"
    caveat = "（价格待人工核算，以下按展示价对比）" if price_uncertain else ""
    risk = compute_cost_risk(our_price, our_cost, basis, margin_floor)

    # 只有促销变化、价格没变时 price_delta 为 None —— 这里必须容忍，
    # 否则「券上线/下线」这类核心场景会抛 AttributeError 让分析任务进死信队列。
    price_delta = price_delta or {}

    if our_price and basis:
        gap = ((our_price - basis) / our_price * 100).quantize(Decimal("0.1"))
        relation = f"低于我方 {our_price} 元约 {gap}%" if gap > 0 else f"高于我方 {our_price} 元"
    else:
        relation = "我方未维护售价，无法比较"

    # 结论分三段拼装：无变化 → 有变化无百分比 → 有变化有百分比
    # （不要用嵌套三目：既难读，也是 B1 那个 None 崩溃的来源）
    if price_delta.get("before") and price_delta.get("after"):
        head = f"竞品「{comp_title[:20]}」{basis_label} {price_delta['before']} → {price_delta['after']}"
        if price_delta.get("pct"):
            head += f"（{Decimal(str(price_delta['pct'])) * 100:.1f}%）"
        conclusion = f"{head}，现价 {basis} 元，{relation}{caveat}"
    else:
        conclusion = f"竞品「{comp_title[:20]}」{basis_label} {basis} 元，{relation}{caveat}"

    if cost_missing or risk.cost_missing:
        action = "先补录自有商品成本，再决定是否跟价；当前不做让利测算"
        impact = "补录成本后可自动算出让利额与打平销量"
        acceptance = "成本录入后重新生成本条提示"
    elif risk.below_floor:
        max_concession = ((risk.margin_before - margin_floor) * (our_price or Decimal("0"))).quantize(Decimal("0.01"))
        if risk.margin_before <= margin_floor:
            # 成本数据本身就有问题（成本 ≥ 售价）：不给金额、不给倍数，直接要求核对
            action = "成本数据异常（成本已吃掉全部毛利），先核对成本与售价再谈跟价"
            impact = "不评估影响：成本口径不清楚时任何结论都不可信"
            acceptance = "成本核对通过后重新生成本条提示"
        else:
            action = (
                f"不要直接跟到 {basis} 元：会让毛利率跌破底线 {margin_floor}。"
                f"单件让利上限约 {max_concession} 元，可用小面额券或搭赠替代"
            )
            impact = risk.note
            acceptance = f"调整后毛利率不低于 {margin_floor}；7 天后复核转化率与毛利额"
    else:
        action = (
            f"人工核算竞品到手价后决定是否跟价。若跟至 {basis} 元："
            f"单件让利 {risk.unit_concession} 元，毛利率 {risk.margin_before} → {risk.margin_after}，"
            f"需销量增至 {risk.breakeven_volume_ratio} 倍打平"
        )
        impact = risk.note or "价格对齐后预期恢复转化率"
        acceptance = f"确认跟价后到手价对齐至 {basis} 元；不跟价则记录原因；7 天后对比转化率"

    card = Card(
        conclusion=conclusion,
        evidence=evidence,
        action=action,
        owner_role="operator",
        deadline=_deadline_for(priority, now),
        expected_impact=impact,
        cost_risk=risk.to_dict() or {"mode": "not_evaluable"},
        acceptance=acceptance,
        kind="B_promo",
    )
    card.violations = compliance_violations(card.conclusion, card.action, card.expected_impact)
    return card
