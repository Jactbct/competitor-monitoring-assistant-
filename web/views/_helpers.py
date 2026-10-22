"""views 通用工具：权限、404 不暴露存在性、HTMX 响应约定。

``views`` 只做「取参 → 调 services → 渲染」，业务分支一律下沉到 services/domain。
"""

from __future__ import annotations

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse

from core.errors import AppError, ErrorCode
from core.log import get_logger
from tenancy.services import audit

__all__ = [
    "client_ip",
    "hx_created",
    "hx_refresh",
    "require_write",
    "scoped_get_or_404",
    "tenant_view",
    "write_required",
]


def tenant_view(view):
    """租户业务页面的统一入口：必须登录、必须有租户、平台运营不可进。

    「平台运营不得看商家生意」这条在代码里体现为**不进租户上下文**，
    而不是靠前端隐藏菜单（补充文档 §8.2）。
    """

    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if getattr(user, "is_platform_ops", False) or user.tenant_id is None:
            log.warning("auth.platform_ops_blocked", extra={"path": request.path})
            raise AppError.of(ErrorCode.AUTH_ROLE_FORBIDDEN, "平台运营账号不能访问租户业务页面")
        return view(request, *args, **kwargs)

    return wrapper

log = get_logger("web")


def client_ip(request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return (request.META.get("REMOTE_ADDR") or "")[:45]


def hx_refresh() -> HttpResponse:
    """让 htmx 整页刷新（写操作后最省事的正确做法）。"""
    return HttpResponse(status=204, headers={"HX-Refresh": "true"})


def hx_created(payload: str = "") -> HttpResponse:
    return HttpResponse(payload, status=200)


def scoped_get_or_404(model, pk):
    """租户内取对象；不属于本租户时**返回 404 并记审计**（不暴露资源存在性）。"""
    obj = model.objects.filter(pk=pk).first()
    if obj is not None:
        return obj
    other = getattr(model, "all_objects", None)
    if other is not None and other.filter(pk=pk).exists():
        audit("tenant.cross_access_blocked", target_type=model.__name__.lower(), target_id=pk)
        log.warning("isolation.blocked", extra={"model": model.__name__, "pk": str(pk)})
    raise Http404("资源不存在")


def write_required(view):
    """需要写权限（admin / operator）。只读角色看不到按钮，服务端也必须拒绝。"""

    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not getattr(request.user, "can_write", False):
            raise AppError.of(ErrorCode.AUTH_ROLE_FORBIDDEN, "当前角色无操作权限")
        return view(request, *args, **kwargs)

    return wrapper


def require_write(request) -> None:
    if not getattr(request.user, "can_write", False):
        raise AppError.of(ErrorCode.AUTH_ROLE_FORBIDDEN, "当前角色无操作权限")
