"""``/health`` —— 外部探针打这个地址（无鉴权，但不泄露任何业务数据）。"""

from __future__ import annotations

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from core.config import get_settings
from jobs.queue import queue_stats

__all__ = ["healthz"]


@require_GET
def healthz(request):
    db_ok = True
    error = ""
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:
        db_ok = False
        error = type(exc).__name__

    try:
        stats = queue_stats()
    except Exception:
        stats = {}

    payload = {
        "status": "ok" if db_ok else "degraded",
        "db": db_ok,
        "error": error,
        "env": get_settings().env,
        "collector_mode": get_settings().collector.mode,
        "llm": get_settings().llm.mode,
        "queue": stats,
    }
    return JsonResponse(payload, status=200 if db_ok else 503)
