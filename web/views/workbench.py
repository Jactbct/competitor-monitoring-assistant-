from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from tenancy.services import list_suggestions, workbench_summary

__all__ = ["alerts", "index"]


@login_required
def index(request):
    if request.user.is_platform_ops:
        return redirect("/ops/health")
    if request.user.tenant_id is None:
        return redirect("/ops/health")
    summary = workbench_summary(str(request.user.tenant_id))
    return render(request, "workbench.html", {"nav": "workbench", "summary": summary})


@login_required
def alerts(request):
    """HTMX 30 秒轮询片段（不整页刷新）。"""
    if request.user.tenant_id is None:
        return render(request, "partials/workbench_alerts.html", {"alerts": []})
    rows = list_suggestions(str(request.user.tenant_id), status="pending")[:8]
    return render(request, "partials/workbench_alerts.html", {"alerts": rows})
