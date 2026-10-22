import os
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import HttpResponseForbidden
from django.shortcuts import render
from django.utils import timezone

from core.config import get_settings
from infra import budget, storage
from jobs.queue import queue_stats
from shared.maintenance import snapshot_volume, table_stats
from shared.models import Snapshot
from tenancy.models import MetricDaily
from tenancy.services import collection_health

__all__ = ["health_page"]


@login_required
def health_page(request):
    """平台运营视图：只给运行指标，**物理上拿不到租户业务数据**（不进租户上下文）。

    越权拦截的明细在审计表里受 RLS 约束，平台运营读不到，因此这里只展示
    worker 汇总过的计数 —— 这本身就是隔离生效的证据。
    """
    if not request.user.is_platform_ops:
        return HttpResponseForbidden("仅平台运营可见")

    settings = get_settings()
    metrics = list(MetricDaily.objects.order_by("-day", "-hour")[:24])

    # B 类闸门看板：7 日到手价可确定率（快照在 shared 层，无 RLS，运维可读）
    since = timezone.now() - timedelta(days=7)
    valid = Snapshot.objects.filter(captured_at__gte=since, status=Snapshot.STATUS_VALID)
    valid_total = valid.count()
    certain = valid.filter(price_confidence="certain").count()
    price_gate = {
        "valid": valid_total,
        "certain": certain,
        "ratio": (certain / valid_total) if valid_total else None,
        "open": valid_total > 0 and certain / valid_total >= 0.70,
        "b_class_enabled": os.environ.get("B_CLASS_ENABLED", "0").strip() in {"1", "true", "yes", "on"},
    }
    blocked_total = int(
        MetricDaily.objects.filter(metric="cross_tenant_blocked").aggregate(v=Sum("value"))["v"] or 0
    )
    return render(
        request,
        "ops_health.html",
        {
            "nav": "ops",
            "health": collection_health(days=7),
            "queue": queue_stats(),
            "budget": budget.stats(),
            "storage": storage.store_stats(),
            "tables": table_stats(),
            "volume": snapshot_volume(),
            "snapshot_retention_days": settings.snapshot_retention_days,
            "price_gate": price_gate,
            "metrics": metrics,
            "blocked_total": blocked_total,
            "settings": settings,
        },
    )
