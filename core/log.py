"""结构化日志（架构文档 §5.2 / 补充 §9.1）。

* 统一 JSON，字段固定：ts / level / service / env / trace_id / event / duration_ms / extra。
* ``trace_id`` 用 contextvar 透传，web → worker → LLM 全链路一致。
* 禁止记录：密码、token、原始 HTML、评价原文、LLM 完整输入输出。
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog

from core.config import get_settings

__all__ = ["bind_trace", "current_trace_id", "get_logger", "new_trace_id", "setup_logging"]

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def current_trace_id() -> str:
    return _trace_id.get()


@contextmanager
def bind_trace(trace_id: str | None = None) -> Iterator[str]:
    """绑定 trace_id，退出时恢复外层值。"""
    token = _trace_id.set(trace_id or new_trace_id())
    try:
        yield _trace_id.get()
    finally:
        _trace_id.reset(token)


def _inject_trace(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict.setdefault("trace_id", _trace_id.get())
    return event_dict


def setup_logging(service: str | None = None) -> None:
    settings = get_settings()
    level = logging.DEBUG if settings.debug else logging.INFO
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_trace,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(
        service=service or os.environ.get("APP_SERVICE", "web"), env=settings.env
    )


def get_logger(event: str = "") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(event or None)
