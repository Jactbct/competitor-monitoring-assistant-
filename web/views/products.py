from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from tenancy.models import SelfProduct
from tenancy.services import audit
from web.views._helpers import require_write, scoped_get_or_404, tenant_view

__all__ = ["create", "index", "update"]


def _dec(raw: str | None):
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _margin_floor(raw, fallback: Decimal) -> Decimal:
    """毛利底线：非法输入回落到原值/默认值，绝不因一个输入框打回 500。"""
    try:
        value = Decimal(str(raw)) if raw not in (None, "") else fallback
    except (InvalidOperation, ValueError):
        return fallback
    if value <= 0 or value >= 1:
        return fallback
    return value


@tenant_view
def index(request):
    tenant_id = str(request.user.tenant_id)
    products = list(SelfProduct.all_objects.filter(tenant_id=tenant_id).order_by("sku"))
    for product in products:
        if product.cost is not None and product.price_display:
            product.margin = ((product.price_display - product.cost) / product.price_display).quantize(
                Decimal("0.001")
            )
            product.margin_below_floor = product.margin < product.margin_floor
        else:
            product.margin = None
            product.margin_below_floor = False
    missing_cost = [p for p in products if p.cost is None]
    return render(
        request,
        "products.html",
        {"nav": "products", "products": products, "missing_cost": len(missing_cost)},
    )


@tenant_view
@require_POST
def create(request):
    require_write(request)
    tenant_id = str(request.user.tenant_id)
    sku = (request.POST.get("sku") or "").strip()
    if not sku:
        messages.error(request, "SKU 必填")
        return redirect("/products")
    product, created = SelfProduct.all_objects.get_or_create(
        tenant_id=tenant_id,
        sku=sku,
        defaults={
            "title": request.POST.get("title") or "",
            "price_display": _dec(request.POST.get("price_display")),
            "cost": _dec(request.POST.get("cost")),
            "margin_floor": _margin_floor(request.POST.get("margin_floor"), Decimal("0.22")),
        },
    )
    if not created:
        messages.error(request, f"SKU {sku} 已存在")
    else:
        audit("product.created", actor=request.user.email, target_type="self_product", target_id=product.id,
              detail={"sku": sku})
        messages.success(
            request,
            "已保存" if product.cost is not None else "已保存，但成本为空：跟价类建议会被降级为「请先补录成本」",
        )
    return redirect("/products")


@tenant_view
@require_POST
def update(request, pk: str):
    require_write(request)
    product = scoped_get_or_404(SelfProduct, pk)
    product.title = request.POST.get("title") or product.title
    product.price_display = _dec(request.POST.get("price_display")) or product.price_display
    # 字段不存在 = 表单没带这一项，**不要**当成「清空成本」：成本为空会直接改写后续跟价建议
    if "cost" in request.POST:
        product.cost = _dec(request.POST.get("cost"))
    product.margin_floor = _margin_floor(request.POST.get("margin_floor"), product.margin_floor)
    product.save(update_fields=["title", "price_display", "cost", "margin_floor"])
    audit("product.updated", actor=request.user.email, target_type="self_product", target_id=product.id,
          detail={"sku": product.sku, "cost": str(product.cost)})
    messages.success(request, "已更新；历史建议不会自动重算，新变化会按新成本测算")
    return redirect("/products")
