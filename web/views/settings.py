from django.contrib import messages
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.config import get_settings
from tenancy.models import AppUser, AuditLog, LlmUsage, TenantTarget
from tenancy.services import audit
from web.views._helpers import require_write, tenant_view

__all__ = ["add_member", "index"]


@tenant_view
def index(request):
    tenant_id = str(request.user.tenant_id)
    members = list(AppUser.objects.filter(tenant_id=tenant_id).order_by("created_at"))
    audits = list(
        AuditLog.objects.filter(tenant=tenant_id).order_by("-created_at")[:30]
    )
    settings = get_settings()
    used = TenantTarget.all_objects.filter(tenant_id=tenant_id, enabled=True).count()

    today = timezone.localdate()
    month_start = today.replace(day=1)

    def _agg(since):
        rows = LlmUsage.all_objects.filter(tenant_id=tenant_id, day__gte=since).aggregate(
            calls=Sum("calls"), tin=Sum("tokens_in"), tout=Sum("tokens_out"), cost=Sum("cost")
        )
        return {
            "calls": rows["calls"] or 0,
            "tokens": int((rows["tin"] or 0) + (rows["tout"] or 0)),
            "cost": rows["cost"] or 0,
        }

    usage_month = _agg(month_start)
    usage_today = _agg(today)
    daily_quota = settings.llm.daily_token_quota
    usage_today["quota"] = daily_quota
    usage_today["remaining"] = max(daily_quota - usage_today["tokens"], 0) if daily_quota else None
    return render(
        request,
        "settings.html",
        {
            "nav": "settings",
            "members": members,
            "audits": audits,
            "roles": AppUser.ROLE_CHOICES[:4],
            "quota": request.user.tenant.target_quota if request.user.tenant else 0,
            "used": used,
            "usage_month": usage_month,
            "usage_today": usage_today,
            "channels": {
                "wecom": bool(settings.notify.wecom_webhook),
                "email": bool(settings.notify.mail_from),
                "llm": settings.llm.mode,
                "collector": settings.collector.mode,
                "qps": settings.collector.qps,
            },
        },
    )


@tenant_view
@require_POST
def add_member(request):
    require_write(request)
    if request.user.role != AppUser.ROLE_ADMIN:
        messages.error(request, "只有管理员可以添加成员")
        return redirect("/settings")
    email = (request.POST.get("email") or "").strip().lower()
    password = request.POST.get("password") or ""
    role = request.POST.get("role") or AppUser.ROLE_OPERATOR
    # 白名单：否则租户管理员 POST role=platform_ops 就能拿到平台视图（横向提权）
    allowed_roles = {value for value, _label in AppUser.ROLE_CHOICES if value != AppUser.ROLE_PLATFORM_OPS}
    if role not in allowed_roles:
        messages.error(request, "角色不合法（platform_ops 只能由运维在命令行创建）")
        return redirect("/settings")
    if not email or len(password) < 8:
        messages.error(request, "邮箱必填，密码至少 8 位")
        return redirect("/settings")
    if AppUser.objects.filter(email=email).exists():
        messages.error(request, "该邮箱已存在")
        return redirect("/settings")
    AppUser.objects.create_user(
        email=email, password=password, tenant=request.user.tenant, role=role, status="active"
    )
    audit("member.added", actor=request.user.email, target_type="app_user", detail={"email": email, "role": role})
    messages.success(request, f"已添加成员 {email}")
    return redirect("/settings")
