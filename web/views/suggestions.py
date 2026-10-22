from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core.errors import AppError
from tenancy.models import Suggestion
from tenancy.services import (
    IGNORE_REASON_TAGS,
    accept_suggestion,
    export_suggestions_csv,
    ignore_suggestion,
    list_suggestions,
    mark_false_positive,
    suggestion_feed_counts,
)
from web.views._helpers import hx_refresh, require_write, scoped_get_or_404, tenant_view

__all__ = ["accept", "detail_panel", "export_csv", "false_positive", "ignore", "index"]


@tenant_view
def index(request):
    tenant_id = str(request.user.tenant_id)
    priority = request.GET.get("priority", "")
    status = request.GET.get("status", "pending")
    kind = request.GET.get("kind", "")
    rows = list_suggestions(tenant_id, priority=priority, status=status, kind=kind)
    return render(
        request,
        "suggestions.html",
        {
            "nav": "suggestions",
            "rows": rows,
            "counts": suggestion_feed_counts(tenant_id),
            "priority": priority,
            "status": status,
            "kind": kind,
            "reason_tags": IGNORE_REASON_TAGS,
        },
    )


@tenant_view
def export_csv(request):
    """导出建议：每份文件带水印，并写审计（FR-05 / §10.4）。"""
    rows = list_suggestions(str(request.user.tenant_id), status=request.GET.get("status", "pending"))
    payload = export_suggestions_csv(
        rows, tenant_name=getattr(request.user.tenant, "name", ""), actor=request.user.email
    )
    response = HttpResponse(payload.encode("utf-8-sig"), content_type="text/csv; charset=utf-8")
    filename = f"suggestions-{request.user.tenant_id}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@tenant_view
def detail_panel(request, pk: str):
    suggestion = scoped_get_or_404(Suggestion, pk)
    analysis = suggestion.analysis
    return render(
        request,
        "partials/suggestion_panel.html",
        {
            "row": suggestion,
            "card": suggestion.card or {},
            "analysis": analysis,
            "reason_tags": IGNORE_REASON_TAGS,
        },
    )


@tenant_view
@require_POST
def accept(request, pk: str):
    require_write(request)
    suggestion = scoped_get_or_404(Suggestion, pk)
    accept_suggestion(suggestion, user=request.user, actor=request.user.email)
    return hx_refresh()


@tenant_view
@require_POST
def ignore(request, pk: str):
    require_write(request)
    suggestion = scoped_get_or_404(Suggestion, pk)
    try:
        ignore_suggestion(
            suggestion,
            user=request.user,
            reason_tag=request.POST.get("reason_tag", ""),
            note=request.POST.get("note", ""),
            actor=request.user.email,
        )
    except AppError as exc:
        return render(
            request,
            "partials/suggestion_panel.html",
            {
                "row": suggestion,
                "card": suggestion.card or {},
                "analysis": suggestion.analysis,
                "reason_tags": IGNORE_REASON_TAGS,
                "error": exc.message,
            },
        )
    return hx_refresh()


@tenant_view
@require_POST
def false_positive(request, pk: str):
    require_write(request)
    suggestion = scoped_get_or_404(Suggestion, pk)
    mark_false_positive(suggestion, user=request.user, note=request.POST.get("note", ""), actor=request.user.email)
    return hx_refresh()
