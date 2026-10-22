"""提示词加载。

提示词存代码库、跟代码一起 review、一起回滚（补充文档 §6.7）。
版本号写进 ``analysis.model_meta``，任何一张历史建议都能追溯到当时的 prompt。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

__all__ = ["PromptBundle", "load_prompts"]

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass(frozen=True)
class PromptBundle:
    version: str
    n1_system: str
    n2_system: str


@lru_cache(maxsize=4)
def load_prompts(version: str = "p1") -> PromptBundle:
    n1 = (_PROMPT_DIR / f"n1_{version}.md").read_text(encoding="utf-8")
    n2 = (_PROMPT_DIR / f"n2_{version}.md").read_text(encoding="utf-8")
    return PromptBundle(version=version, n1_system=n1, n2_system=n2)
