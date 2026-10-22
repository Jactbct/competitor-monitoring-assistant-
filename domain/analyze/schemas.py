"""LLM 节点的 I/O schema（pydantic）。

三道硬校验都长在代码里，不靠提示词（补充文档 §6.3）：
1. 证据可追溯 —— ``evidence_text`` 必须是原文归一化后的子串；
2. 证据 ID 存在 —— ``evidence_refs`` 必须能在快照里查到；
3. 数字以规则为准 —— 与规则引擎逐字段比对，冲突取规则值。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "AnalysisOutput",
    "PromoExtraction",
    "PromoItemOut",
    "ATTRIBUTIONS",
    "THREAT_LEVELS",
]

ATTRIBUTIONS = (
    "活动开始",
    "活动结束",
    "真实调价",
    "SKU结构变更",
    "页面改版",
    "券力度变化",
)
THREAT_LEVELS = ("high", "medium", "low")


class PromoItemOut(BaseModel):
    activity_type: Literal["满减", "每满减", "立减", "秒杀", "店铺券", "平台券", "赠品", "无"]
    threshold_yuan: Decimal | None = Field(default=None, ge=0, le=100000)
    discount_yuan: Decimal | None = Field(default=None, ge=0, le=100000)
    repeat_tiers: int | None = Field(default=None, ge=-1, le=10)
    time_window_text: str | None = Field(default=None, max_length=60)
    evidence_text: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)

    @field_validator("evidence_text")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence_text 不能为空白")
        return value


class PromoExtraction(BaseModel):
    items: list[PromoItemOut] = Field(default_factory=list, max_length=8)
    page_noise_detected: bool = False


class AnalysisOutput(BaseModel):
    attribution: Literal["活动开始", "活动结束", "真实调价", "SKU结构变更", "页面改版", "券力度变化"]
    attribution_confidence: float = Field(ge=0, le=1)
    threat_level: Literal["high", "medium", "low"]
    threat_reason: str = Field(max_length=200)
    kind: Literal["A_info", "B_promo", "C_creative", "none"]
    evidence_refs: list[str] = Field(min_length=1)
