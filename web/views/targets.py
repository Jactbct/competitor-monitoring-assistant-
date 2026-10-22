from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core.errors import AppError
from tenancy.models import SelfProduct, TenantTarget
from tenancy.services import (
    add_target,
    delete_binding,
    list_bindings,
    set_priority,
    target_detail,
)
from web.views._helpers import hx_refresh, require_write, scoped_get_or_404, tenant_view

__all__ = ["create", "detail", "index", "remove", "update_priority"]


def _products(tenant_id: str):
    return list(SelfProduct.all_objects.filter(tenant_id=tenant_id).order_by("sku"))


@tenant_view
def index(request):
    tenant_id = str(request.user.tenant_id)
    priority = request.GET.get("priority", "")
    keyword = request.GET.get("q", "").strip()
    rows = list_bindings(tenant_id, priority=priority, keyword=keyword)
    return render(
        request,
        "targets.html",
        {
            "nav": "targets",
            "rows": rows,
            "products": _products(tenant_id),
            "priority": priority,
            "keyword": keyword,
            "quota": request.user.tenant.target_quota if request.user.tenant else 0,
            # 配额要按全量算，不能按「筛选后的行数」（否则筛一下配额就变小了）
            "used": TenantTarget.all_objects.filter(tenant_id=tenant_id, enabled=True).count(),
        },
    )


@tenant_view
@require_POST
def create(request):
    require_write(request)
    tenant = request.user.tenant
    product_id = request.POST.get("self_product_id") or None
    product = SelfProduct.all_objects.filter(pk=product_id, tenant_id=tenant.id).first() if product_id else None
    try:
        add_target(
            tenant,
            url=request.POST.get("url", ""),
            priority=request.POST.get("priority") or TenantTarget.PRIORITY_NORMAL,
            self_product=product,
            user=request.user,
            actor=request.user.email,
        )
        messages.success(request, "已加入监控，首次采集会在 1 分钟内开始")
    except AppError as exc:
        messages.error(request, exc.message)
    return redirect("/targets")


@tenant_view
@require_POST
def update_priority(request, pk: str):
    require_write(request)
    binding = scoped_get_or_404(TenantTarget, pk)
    try:
        set_priority(binding, request.POST.get("priority", ""), actor=request.user.email)
    except AppError as exc:
        messages.error(request, exc.message)
    return hx_refresh()


@tenant_view
@require_POST
def remove(request, pk: str):
    require_write(request)
    binding = scoped_get_or_404(TenantTarget, pk)
    delete_binding(binding, actor=request.user.email)
    return hx_refresh()


@tenant_view
def detail(request, pk: str):
    binding = scoped_get_or_404(TenantTarget, pk)
    if request.tenant_id and str(binding.tenant_id) != str(request.tenant_id):
        raise Http404("资源不存在")
    ctx = target_detail(str(request.user.tenant_id), binding)
    ctx.update({"nav": "targets"})
    return render(request, "target_detail.html", ctx)
