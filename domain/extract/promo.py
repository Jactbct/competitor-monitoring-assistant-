"""促销文案解析（规则引擎）。

这是防幻觉体系的**对照系**：LLM 抽出的金额/门槛必须能在这里复算出来，
冲突取规则值（补充文档 §6.2）。也用于在没有 LLM 时独立产出促销明细。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from domain.extract.text import normalize

__all__ = ["PromoItem", "parse_promos", "promo_signature"]

#: 活动类型（与 pydantic schema 的枚举保持一致，顺序即「同一层只能叠一种」的优先级）
TYPE_FULL_REDUCE = "满减"
TYPE_EACH_FULL_REDUCE = "每满减"
TYPE_INSTANT = "立减"
TYPE_SECKILL = "秒杀"
TYPE_SHOP_COUPON = "店铺券"
TYPE_PLATFORM_COUPON = "平台券"
TYPE_GIFT = "赠品"

_NUM = r"(\d+(?:\.\d{1,2})?)"

# 顺序有意义：更具体的模式先匹配，避免「每满 200 减 30」被「满减」抢先
_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (TYPE_EACH_FULL_REDUCE, re.compile(rf"每满\s*{_NUM}\s*元?\s*减\s*{_NUM}"), "threshold,discount"),
    # 负向先行断言：避免「每满200减30」同时被「满200减30」命中，造成假冲突
    (TYPE_FULL_REDUCE, re.compile(rf"(?<!每)满\s*{_NUM}\s*元?\s*减\s*{_NUM}"), "threshold,discount"),
    (TYPE_SECKILL, re.compile(rf"秒杀价?\s*[¥￥]?\s*{_NUM}"), "price"),
    (TYPE_INSTANT, re.compile(rf"(?<!每)立减\s*[¥￥]?\s*{_NUM}"), "discount"),
    (TYPE_SHOP_COUPON, re.compile(rf"(?:店铺券?|本店券?)[^0-9]{{0,6}}{_NUM}\s*元"), "discount"),
    (TYPE_PLATFORM_COUPON, re.compile(rf"(?:平台券?|跨店满减)[^0-9]{{0,6}}{_NUM}\s*元"), "discount"),
    (TYPE_GIFT, re.compile(r"买\s*\d+\s*送\s*\d+|赠品|加赠"), "none"),
)

_SENTENCE_SPLIT = re.compile(r"[。；;\n|]")


@dataclass(frozen=True)
class PromoItem:
    activity_type: str
    threshold_yuan: Decimal | None = None
    discount_yuan: Decimal | None = None
    repeat_tiers: int | None = None
    sekill_price: Decimal | None = None
    evidence_text: str = ""
    confidence: float = 1.0
    source: str = "rules"  # rules|llm

    def to_dict(self) -> dict:
        return {
            "activity_type": self.activity_type,
            "threshold_yuan": str(self.threshold_yuan) if self.threshold_yuan is not None else None,
            "discount_yuan": str(self.discount_yuan) if self.discount_yuan is not None else None,
            "repeat_tiers": self.repeat_tiers,
            # 秒杀价是到手价的基准价，不序列化就没法从证据链复算
            "seckill_price": str(self.sekill_price) if self.sekill_price is not None else None,
            "time_window_text": None,
            "evidence_text": self.evidence_text,
            "confidence": self.confidence,
        }

    def key(self) -> tuple:
        return (
            self.activity_type,
            str(self.threshold_yuan) if self.threshold_yuan is not None else "",
            str(self.discount_yuan) if self.discount_yuan is not None else "",
        )


@dataclass
class PromoParseResult:
    items: list[PromoItem] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def _sentences(text: str) -> list[str]:
    return [s for s in (normalize(p) for p in _SENTENCE_SPLIT.split(text)) if s]


def parse_promos(raw_texts: list[str]) -> PromoParseResult:
    """从促销区文案里解析活动项。每个 item 带原文句子作为证据。"""
    result = PromoParseResult()
    seen: set[tuple] = set()

    for raw in raw_texts:
        for sentence in _sentences(raw):
            for activity_type, pattern, shape in _PATTERNS:
                for match in pattern.finditer(sentence):
                    if shape == "threshold,discount":
                        threshold = Decimal(match.group(1))
                        discount = Decimal(match.group(2))
                        if discount <= 0 or threshold <= 0 or discount >= threshold * 2:
                            continue
                        item = PromoItem(
                            activity_type=activity_type,
                            threshold_yuan=threshold,
                            discount_yuan=discount,
                            repeat_tiers=None,
                            evidence_text=sentence,
                        )
                    elif shape == "discount":
                        discount = Decimal(match.group(1))
                        if discount <= 0 or discount > 5000:
                            continue
                        item = PromoItem(
                            activity_type=activity_type, discount_yuan=discount, evidence_text=sentence
                        )
                    elif shape == "price":
                        price = Decimal(match.group(1))
                        if price <= 0 or price > 100000:
                            continue
                        item = PromoItem(
                            activity_type=activity_type, sekill_price=price, evidence_text=sentence
                        )
                    else:
                        item = PromoItem(activity_type=activity_type, evidence_text=sentence)

                    # 「店铺券 满199减20」会被满减模式先命中；按句子语境纠正类型，
                    # 否则券与满减的叠加判断会失真（券通常不可与同门槛满减叠加）。
                    if item.activity_type == TYPE_FULL_REDUCE and "券" in sentence:
                        coupon_type = (
                            TYPE_SHOP_COUPON
                            if ("店铺" in sentence or "本店" in sentence or "粉丝" in sentence)
                            else TYPE_PLATFORM_COUPON
                        )
                        item = PromoItem(
                            activity_type=coupon_type,
                            threshold_yuan=item.threshold_yuan,
                            discount_yuan=item.discount_yuan,
                            evidence_text=item.evidence_text,
                        )

                    if item.key() in seen:
                        continue
                    seen.add(item.key())
                    result.items.append(item)
                    # 「每满减」保留 repeat_tiers 语义（无上限，按档位累计）
                    if activity_type == TYPE_EACH_FULL_REDUCE:
                        result.items[-1] = PromoItem(
                            activity_type=item.activity_type,
                            threshold_yuan=item.threshold_yuan,
                            discount_yuan=item.discount_yuan,
                            repeat_tiers=-1,
                            evidence_text=item.evidence_text,
                        )

    # 冲突识别：同层多种可叠加规则并存 → 到手价不可确定
    layers: dict[str, list[PromoItem]] = {}
    for item in result.items:
        layers.setdefault(item.activity_type, []).append(item)
    if len(layers.get(TYPE_FULL_REDUCE, [])) > 1:
        result.conflicts.append("多个不同门槛的满减并存，无法判断实际生效档位")
    if TYPE_FULL_REDUCE in layers and TYPE_EACH_FULL_REDUCE in layers:
        result.conflicts.append("满减与每满减并存，叠加规则不明")
    if len(layers.get(TYPE_SHOP_COUPON, [])) > 1:
        result.conflicts.append("多张店铺券并存，是否可叠加不明")
    if TYPE_SECKILL in layers and TYPE_FULL_REDUCE in layers:
        result.conflicts.append("秒杀价与满减并存，秒杀是否参与满减不明")

    return result


def promo_signature(items: list[PromoItem]) -> str:
    """促销指纹：用于 diff 判断「促销是否变化」。"""
    return "|".join(sorted(",".join(i.key()) for i in items)) or "none"
