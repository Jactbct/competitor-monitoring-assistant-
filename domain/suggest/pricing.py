"""毛利与打平测算（本项目最核心的业务公式，原型已验证，改动需带回归测试）。

「跟价到 X 元要卖多少件才能打平」是运营唯一真正关心的数字。
缺成本时**不出跟价建议**（EX-07），只出「请先补录成本」引导卡。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

__all__ = ["CostRisk", "compute_cost_risk"]

_Q3 = Decimal("0.001")
_Q2 = Decimal("0.01")


@dataclass
class CostRisk:
    unit_concession: Decimal | None
    margin_before: Decimal | None
    margin_after: Decimal | None
    margin_floor: Decimal
    breakeven_volume_ratio: Decimal | None
    below_floor: bool
    cost_missing: bool
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "unit_concession": _s(self.unit_concession),
            "margin_before": _s(self.margin_before),
            "margin_after": _s(self.margin_after),
            "margin_floor": _s(self.margin_floor),
            "breakeven_volume_ratio": _s(self.breakeven_volume_ratio),
            "below_floor": self.below_floor,
            "cost_missing": self.cost_missing,
            "note": self.note,
        }


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _margin(price: Decimal, cost: Decimal) -> Decimal:
    if price <= 0:
        return Decimal("0")
    return ((price - cost) / price).quantize(_Q3, rounding=ROUND_HALF_UP)


def compute_cost_risk(
    our_price: Decimal | None,
    cost: Decimal | None,
    target_price: Decimal | None,
    margin_floor: Decimal = Decimal("0.22"),
) -> CostRisk:
    """算让利、前后毛利率、打平所需销量倍数。

    * ``unit_concession``：跟到对手到手价，我方单件要让多少（对手更贵则为负，即无需让利）。
    * ``breakeven_volume_ratio``：毛利率下降后，需要多少倍销量才能维持原来的单件总毛利。
      公式 ``margin_before / margin_after``；若会让利到毛利率 ≤0，则不给倍数（无解）。
    """
    if cost is None or our_price is None or target_price is None:
        return CostRisk(
            unit_concession=None,
            margin_before=None,
            margin_after=None,
            margin_floor=margin_floor,
            breakeven_volume_ratio=None,
            below_floor=False,
            cost_missing=True,
            note="缺少自有商品价格或成本，无法测算（EX-07）",
        )

    concession = (our_price - target_price).quantize(_Q2, rounding=ROUND_HALF_UP)
    margin_before = _margin(our_price, cost)
    margin_after = _margin(target_price, cost)

    ratio: Decimal | None = None
    if target_price < our_price:
        if margin_after > 0:
            ratio = (margin_before / margin_after).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            ratio = None

    below_floor = margin_after < margin_floor
    note = ""
    if below_floor:
        note = f"跟价后毛利率 {margin_after} 低于底线 {margin_floor}，需审批"
    elif ratio is not None:
        note = f"需销量增至 {ratio} 倍方可打平（估算，假设转化率不变）"

    return CostRisk(
        unit_concession=concession,
        margin_before=margin_before,
        margin_after=margin_after,
        margin_floor=margin_floor,
        breakeven_volume_ratio=ratio,
        below_floor=below_floor,
        cost_missing=False,
        note=note,
    )
