from django.shortcuts import render
from django.views.decorators.http import require_POST

from tenancy.models import AppUser, Task
from tenancy.services import audit
from web.views._helpers import hx_refresh, require_write, scoped_get_or_404, tenant_view

__all__ = ["index", "update_status"]

_COLUMNS = (
    (Task.STATUS_TODO, "待开始"),
    (Task.STATUS_DOING, "进行中"),
    (Task.STATUS_DONE, "已完成"),
)


@tenant_view
def index(request):
    tenant_id = str(request.user.tenant_id)
    from tenancy.models import Suggestion

    tasks = list(Task.all_objects.filter(tenant_id=tenant_id).order_by("-created_at")[:200])
    # 一次批量取，而不是每个任务查一次（原来最多 200 次查询）
    suggestion_ids = [t.suggestion_id for t in tasks if t.suggestion_id]
    suggestions = Suggestion.all_objects.in_bulk(suggestion_ids)
    columns = []
    for status, label in _COLUMNS:
        items = [t for t in tasks if t.status == status]
        for item in items:
            item.card = (suggestions.get(item.suggestion_id).card if suggestions.get(item.suggestion_id) else {}) or {}
        columns.append({"status": status, "label": label, "items": items})
    members = {m.id: m for m in AppUser.objects.filter(tenant_id=tenant_id)}
    return render(
        request,
        "tasks.html",
        {"nav": "tasks", "columns": columns, "total": len(tasks), "members": members},
    )


@tenant_view
@require_POST
def update_status(request, pk: str):
    require_write(request)
    task = scoped_get_or_404(Task, pk)
    status = request.POST.get("status", "")
    if status in dict(_COLUMNS):
        task.status = status
        task.save(update_fields=["status", "updated_at"])
        audit("task.status_changed", actor=request.user.email, target_type="task", target_id=task.id,
              detail={"status": status})
    return hx_refresh()
