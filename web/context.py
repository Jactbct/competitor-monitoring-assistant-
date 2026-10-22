"""模板上下文：导航高亮、待处理计数、环境标识。"""

from __future__ import annotations

from core.config import get_settings
from tenancy.models import Suggestion

__all__ = ["nav_context"]


def nav_context(request) -> dict:
    settings = get_settings()
    user = getattr(request, "user", None)
    tenant = None
    p0 = 0
    if user is not None and user.is_authenticated and not user.is_platform_ops:
        try:
            tenant = user.tenant
        except Exception:  # 租户被删但会话还在
            tenant = None
        if not getattr(request, "_nav_p0_cached", False):
            p0 = Suggestion.objects.filter(
                priority="P0", status=Suggestion.STATUS_PENDING
            ).count()
            request._nav_p0_cached = True
            request._nav_p0_value = p0
        else:
            p0 = getattr(request, "_nav_p0_value", 0)

    return {
        "tenant": tenant,
        "nav_p0": p0,
        "collect_mode": settings.collector.mode,
        "qps": settings.collector.qps,
        "app_env": settings.env,
    }
