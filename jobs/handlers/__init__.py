"""handler 注册表：worker 只按 kind 分发，不 import 具体业务。"""

from __future__ import annotations

from collections.abc import Callable

from jobs.handlers.analyze import handle_analyze
from jobs.handlers.collect import handle_collect, handle_schedule_tick
from jobs.handlers.health import handle_daily_digest, handle_health_snapshot, handle_silence_check
from jobs.handlers.notify import handle_notify

__all__ = ["HANDLERS", "get_handler"]

HANDLERS: dict[str, Callable[[dict], dict]] = {
    "schedule_tick": handle_schedule_tick,
    "collect": handle_collect,
    "analyze": handle_analyze,
    "notify": handle_notify,
    "health_snapshot": handle_health_snapshot,
    "silence_check": handle_silence_check,
    "daily_digest": handle_daily_digest,
}


def get_handler(kind: str) -> Callable[[dict], dict] | None:
    return HANDLERS.get(kind)
