"""LLM 端口（依赖倒置）。

``domain/analyze`` 不直接发 HTTP：适配器在 :mod:`infra.llm`，测试里注入假实现即可
—— 这让「证据校验 / 规则回算 / 降级」这些最关键的逻辑可以完全离线测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

__all__ = ["LlmResult", "LlmPort"]


@dataclass
class LlmResult:
    data: dict
    model: str = ""
    node: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost: Decimal = Decimal("0")
    cached: bool = False
    prompt_version: str = ""

    def meta(self) -> dict:
        return {
            "model": self.model,
            "node": self.node,
            "prompt_version": self.prompt_version,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost": str(self.cost),
            "cached": self.cached,
        }


class LlmPort(Protocol):
    def complete_json(
        self, *, node: str, system: str, user: str, model: str, cache_material: str, tenant_id: str | None = None
    ) -> LlmResult: ...
