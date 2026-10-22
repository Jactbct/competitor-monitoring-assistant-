"""变化检测：指纹、字段 diff、抖动合并。全部纯函数。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from domain.extract.text import normalize

__all__ = ["DEFAULT_JITTER_WINDOW", "DiffResult", "diff", "fingerprint", "merge_jitter"]

DEFAULT_JITTER_WINDOW = timedelta(minutes=30)

#: 快照字段 → 事件类型
FIELD_TO_EVENT = {
    "price_final": "real_price",
    "price_display": "real_price",
    "promo_signature": "coupon_promo",
    "title": "title",
    "main_image": "main_image",
    "sku_count": "sku_structure",
    "attrs": "sku_structure",
}

#: 只影响展示、不触发建议的字段（避免大量噪音卡片）
_LOW_SIGNAL_FIELDS = {"attrs", "sku_count", "shop_name"}


@dataclass
class DiffResult:
    changed_fields: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    deltas: dict = field(default_factory=dict)
    magnitude: Decimal | None = None
    significant: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.changed_fields

    def to_dict(self) -> dict:
        return {
            "changed_fields": self.changed_fields,
            "event_types": self.event_types,
            "deltas": self.deltas,
            "magnitude": str(self.magnitude) if self.magnitude is not None else None,
            "significant": self.significant,
        }


def _canonical(value) -> str:
    if isinstance(value, dict):
        return json.dumps({k: value[k] for k in sorted(value)}, ensure_ascii=False, sort_keys=True, default=str)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return normalize(str(value)) if value is not None else ""


def fingerprint(fields: dict) -> str:
    """内容指纹：只在「有业务含义」的字段上计算，避免时间戳/无关属性造成伪变化。"""
    keys = ("title", "price_display", "price_final", "main_image", "sku_count", "promo_signature")
    payload = "|".join(f"{k}={_canonical(fields.get(k))}" for k in keys)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def diff(prev: dict, cur: dict) -> DiffResult:
    """字段级 diff。``prev`` / ``cur`` 都是快照的 fields 字典。"""
    result = DiffResult()
    for key in (
        "title",
        "price_display",
        "price_final",
        "promo_signature",
        "main_image",
        "sku_count",
        "attrs",
    ):
        before = prev.get(key)
        after = cur.get(key)
        if _canonical(before) == _canonical(after):
            continue
        result.changed_fields.append(key)
        event_type = FIELD_TO_EVENT.get(key)
        if event_type and event_type not in result.event_types:
            result.event_types.append(event_type)

        if key in {"price_display", "price_final"}:
            b, a = _dec(before), _dec(after)
            entry: dict = {"before": str(b) if b is not None else None, "after": str(a) if a is not None else None}
            if b is not None and a is not None and b > 0:
                change = (a - b) / b
                entry["pct"] = f"{change.quantize(Decimal('0.0001'))}"
                if key == "price_final":
                    result.magnitude = abs(change).quantize(Decimal("0.0001"))
            result.deltas[key] = entry
        else:
            result.deltas[key] = {
                "before": _truncate(before),
                "after": _truncate(after),
            }

    if result.magnitude is None and result.changed_fields:
        # 非价格变化也要有可比较的幅度，否则 core 种子的标题/促销变化永远进不了 P0
        if "title" in result.changed_fields or "main_image" in result.changed_fields:
            result.magnitude = Decimal("0.0500")
        elif "promo_signature" in result.changed_fields:
            result.magnitude = Decimal("0.0300")
        else:
            result.magnitude = Decimal("0.0100")

    # 只有强信号才算「值得出建议」：价格 / 促销 / 标题 / 主图变化
    strong = result.event_types and any(t in {"real_price", "coupon_promo", "title", "main_image"} for t in result.event_types)
    low_only = all(f in _LOW_SIGNAL_FIELDS for f in result.changed_fields)
    result.significant = bool(strong and not low_only)
    return result


def _truncate(value, limit: int = 120):
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: str(v)[:40] for k, v in list(value.items())[:8]}
    text = str(value)
    return text[:limit]


def merge_jitter(
    events: list[tuple[datetime, Decimal | None]], window: timedelta = DEFAULT_JITTER_WINDOW
) -> list[tuple[datetime, Decimal | None, int]]:
    """同一目标在窗口内价格往复（129→119→129）合并成一个事件，避免刷屏。

    输入按时间升序，输出 ``(首见时间, 净幅度, 合并条数)``；净幅度为窗口内
    「末值相对首值」的变化率，但**保留中间的最大波动**用于威胁判定。
    """
    if not events:
        return []
    merged: list[tuple[datetime, Decimal | None, int]] = []
    bucket_start, first_value = events[0]
    last_value = first_value
    count = 1
    peak = Decimal("0")

    for timestamp, value in events[1:]:
        if timestamp - bucket_start <= window:
            count += 1
            if first_value not in (None, 0) and isinstance(value, Decimal):
                peak = max(peak, abs((value - first_value) / first_value))
            last_value = value
            continue
        merged.append(_emit(bucket_start, first_value, last_value, count, peak))
        bucket_start, first_value, last_value, count, peak = timestamp, value, value, 1, Decimal("0")

    merged.append(_emit(bucket_start, first_value, last_value, count, peak))
    return merged


def _emit(start, first, last, count, peak) -> tuple[datetime, Decimal | None, int]:
    net: Decimal | None = None
    if isinstance(first, Decimal) and isinstance(last, Decimal) and first > 0:
        net = ((last - first) / first).quantize(Decimal("0.0001"))
    if net is not None and peak > abs(net):
        net = (peak if net >= 0 else -peak).quantize(Decimal("0.0001"))
    return start, net, count
