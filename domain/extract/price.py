"""到手价求解器（确定性规则，**绝不估算**）。

PRD FR-24 / K-9：无法确定时必须标 ``uncertain``，禁止用「大概」「约」估算。
这个模块出过 bug（原型 92.5% → 100%），所以它必须有集成测试守着。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from domain.extract.promo import (
    TYPE_EACH_FULL_REDUCE,
    TYPE_FULL_REDUCE,
    TYPE_INSTANT,
    TYPE_PLATFORM_COUPON,
    TYPE_SECKILL,
    TYPE_SHOP_COUPON,
    PromoItem,
)

__all__ = ["CONFIDENCE_CERTAIN", "CONFIDENCE_UNCERTAIN", "PriceResult", "compute_final_price"]

CONFIDENCE_CERTAIN = "certain"
CONFIDENCE_UNCERTAIN = "uncertain"

_CENT = Decimal("0.01")
_MIN_PAYABLE = Decimal("0.01")


@dataclass
class PriceLine:
    """到手价明细的一行，直接回链给用户看（PRD FR-24 要求逐项明细）。"""

    label: str
    activity_type: str
    amount: Decimal
    applied: bool
    note: str = ""
    evidence_text: str = ""

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "activity_type": self.activity_type,
            "amount": str(self.amount),
            "applied": self.applied,
            "note": self.note,
            "evidence_text": self.evidence_text,
        }


@dataclass
class PriceResult:
    display: Decimal | None
    final: Decimal | None
    confidence: str
    lines: list[PriceLine] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def is_uncertain(self) -> bool:
        return self.confidence == CONFIDENCE_UNCERTAIN

    def to_dict(self) -> dict:
        return {
            "display": str(self.display) if self.display is not None else None,
            "final": str(self.final) if self.final is not None else None,
            "confidence": self.confidence,
            "lines": [line.to_dict() for line in self.lines],
            "reasons": self.reasons,
        }


def _best_full_reduce(items: list[PromoItem], base: Decimal) -> tuple[PromoItem | None, list[PromoItem]]:
    applicable = [i for i in items if i.threshold_yuan is not None and base >= i.threshold_yuan]
    if not applicable:
        return None, items
    best = max(applicable, key=lambda i: i.discount_yuan or Decimal("0"))
    return best, [i for i in items if i is not best]


def compute_final_price(display: Decimal | None, promos: list[PromoItem]) -> PriceResult:
    """展示价 + 促销规则 → 到手价。

    叠加顺序（与平台实际行为一致）：秒杀价定基准 → 满减/每满减 → 券。
    任何一步存在歧义（多个同层规则、跨层叠加不明），结论标 ``uncertain``，
    但仍给出「按保守解读」的数值，方便人工核算。
    """
    if display is None or display <= 0:
        # 0 元更可能是「没抓到价」而不是真免费；按缺失处理，绝不用 0.01 兜底假装算出来了
        return PriceResult(
            display=None,
            final=None,
            confidence=CONFIDENCE_UNCERTAIN,
            reasons=["未取到有效展示价"],
        )

    reasons: list[str] = []
    lines: list[PriceLine] = []
    base = display.quantize(_CENT)

    by_type: dict[str, list[PromoItem]] = {}
    for item in promos:
        by_type.setdefault(item.activity_type, []).append(item)

    # ① 秒杀价：直接替换基准价（比展示价低才生效）
    for item in by_type.get(TYPE_SECKILL, []):
        price = item.sekill_price
        if price is None:
            continue
        if price < base:
            lines.append(
                PriceLine(
                    label="秒杀价",
                    activity_type=TYPE_SECKILL,
                    amount=(base - price).quantize(_CENT),
                    applied=True,
                    note=f"基准价 {base} → {price.quantize(_CENT)}",
                    evidence_text=item.evidence_text,
                )
            )
            base = price.quantize(_CENT)
        else:
            lines.append(
                PriceLine(
                    label="秒杀价（不生效）",
                    activity_type=TYPE_SECKILL,
                    amount=Decimal("0.00"),
                    applied=False,
                    note="秒杀价不低于展示价，判为噪音",
                    evidence_text=item.evidence_text,
                )
            )

    total_discount = Decimal("0.00")

    # ② 满减 / 每满减
    full_items = by_type.get(TYPE_FULL_REDUCE, [])
    each_items = by_type.get(TYPE_EACH_FULL_REDUCE, [])
    if len(full_items) > 1:
        reasons.append("多个不同门槛的满减并存，按最优档（保守）计算，实际生效档位待确认")
    if full_items and each_items:
        reasons.append("满减与每满减并存，按满减（保守）计算，到手价不确定")
        for item in each_items:
            lines.append(
                PriceLine(
                    label=f"每满 {item.threshold_yuan} 减 {item.discount_yuan}（与满减并存，未参与计算）",
                    activity_type=TYPE_EACH_FULL_REDUCE,
                    amount=Decimal("0.00"),
                    applied=False,
                    note="规则冲突，按保守口径只计满减",
                    evidence_text=item.evidence_text,
                )
            )

    if full_items:
        best, rejected = _best_full_reduce(full_items, base)
        if best is not None:
            for item in rejected:
                lines.append(
                    PriceLine(
                        label=f"满 {item.threshold_yuan} 减 {item.discount_yuan}（未达最优档）",
                        activity_type=TYPE_FULL_REDUCE,
                        amount=Decimal("0.00"),
                        applied=False,
                        evidence_text=item.evidence_text,
                    )
                )
            amount = (best.discount_yuan or Decimal("0")).quantize(_CENT)
            total_discount += amount
            lines.append(
                PriceLine(
                    label=f"满 {best.threshold_yuan} 减 {best.discount_yuan}",
                    activity_type=TYPE_FULL_REDUCE,
                    amount=amount,
                    applied=True,
                    evidence_text=best.evidence_text,
                )
            )
        else:
            # 门槛未达到是**确定性结论**，不是不确定；只记明细，不加 reason
            for item in full_items:
                lines.append(
                    PriceLine(
                        label=f"满 {item.threshold_yuan} 减 {item.discount_yuan}（未达门槛）",
                        activity_type=TYPE_FULL_REDUCE,
                        amount=Decimal("0.00"),
                        applied=False,
                        evidence_text=item.evidence_text,
                    )
                )

    if each_items and not full_items:
        for item in each_items:
            threshold = item.threshold_yuan or Decimal("0")
            if threshold <= 0 or base < threshold:
                lines.append(
                    PriceLine(
                        label=f"每满 {threshold} 减 {item.discount_yuan}（未达门槛）",
                        activity_type=TYPE_EACH_FULL_REDUCE,
                        amount=Decimal("0.00"),
                        applied=False,
                        evidence_text=item.evidence_text,
                    )
                )
                continue
            tiers = int(base // threshold)
            amount = ((item.discount_yuan or Decimal("0")) * tiers).quantize(_CENT)
            total_discount += amount
            lines.append(
                PriceLine(
                    label=f"每满 {threshold} 减 {item.discount_yuan} ×{tiers}",
                    activity_type=TYPE_EACH_FULL_REDUCE,
                    amount=amount,
                    applied=True,
                    evidence_text=item.evidence_text,
                )
            )

    # ③ 立减
    instant = by_type.get(TYPE_INSTANT, [])
    if len(instant) > 1:
        reasons.append("多条立减并存，是否叠加不明")
    for item in instant:
        amount = (item.discount_yuan or Decimal("0")).quantize(_CENT)
        if amount <= 0:
            continue
        total_discount += amount
        lines.append(
            PriceLine(
                label=f"立减 {amount}",
                activity_type=TYPE_INSTANT,
                amount=amount,
                applied=True,
                evidence_text=item.evidence_text,
            )
        )

    # ④ 券（同层只叠加一张；多张即不确定）
    if len(by_type.get(TYPE_PLATFORM_COUPON, [])) > 1:
        reasons.append("多张平台券并存，是否可叠加不明")
    for coupon_type in (TYPE_PLATFORM_COUPON, TYPE_SHOP_COUPON):
        for item in by_type.get(coupon_type, []):
            amount = (item.discount_yuan or Decimal("0")).quantize(_CENT)
            if amount <= 0:
                continue
            # 关键：每张券都要按**当前**应付金额判门槛。
            # 写成循环外一次性计算，平台券扣完后店铺券仍用旧值判门槛 → 系统性低估到手价。
            payable = base - total_discount
            threshold = item.threshold_yuan or Decimal("0")
            if threshold and payable < threshold:
                lines.append(
                    PriceLine(
                        label=f"{coupon_type} 满 {threshold} 减 {amount}（未达门槛）",
                        activity_type=coupon_type,
                        amount=Decimal("0.00"),
                        applied=False,
                        note=f"券前应付 {payable}",
                        evidence_text=item.evidence_text,
                    )
                )
                continue
            total_discount += amount
            lines.append(
                PriceLine(
                    label=f"{coupon_type} {amount}",
                    activity_type=coupon_type,
                    amount=amount,
                    applied=True,
                    evidence_text=item.evidence_text,
                )
            )

    final = (base - total_discount).quantize(_CENT)
    if final < _MIN_PAYABLE:
        reasons.append("按规则折后低于 0.01 元，判为规则冲突")
        final = _MIN_PAYABLE

    confidence = CONFIDENCE_CERTAIN
    if reasons:
        confidence = CONFIDENCE_UNCERTAIN

    return PriceResult(
        display=display.quantize(_CENT),
        final=final,
        confidence=confidence,
        lines=lines,
        reasons=reasons,
    )
