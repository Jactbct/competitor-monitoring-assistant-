"""登录 / 退出。

不用 JWT：session 走 httpOnly + SameSite=Lax Cookie，天然不可被 JS 读取，
省掉 access/refresh 轮换、续期竞态、前端刷新重放三块代码（架构文档 §8.3）。
登录失败 5 次锁 15 分钟，计数**落库**（多进程有效）。
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.log import get_logger
from tenancy.models import LoginThrottle
from tenancy.services import audit
from web.views._helpers import client_ip

__all__ = ["login_view", "logout_view"]

log = get_logger("auth")

MAX_FAILS = 5
LOCK_MINUTES = 15


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect(reverse("workbench"))

    error = ""
    email = ""
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        password = request.POST.get("password") or ""

        throttle = LoginThrottle.objects.filter(email=email).first()
        if throttle and throttle.locked_until and throttle.locked_until > timezone.now():
            wait = int((throttle.locked_until - timezone.now()).total_seconds() // 60) + 1
            error = f"登录失败次数过多，请 {wait} 分钟后再试"

        if not error:
            user = authenticate(request, username=email, password=password)
            if user is None:
                count = (throttle.fail_count if throttle else 0) + 1
                locked_until = (
                    timezone.now() + timedelta(minutes=LOCK_MINUTES) if count >= MAX_FAILS else None
                )
                LoginThrottle.objects.update_or_create(
                    email=email, defaults={"fail_count": count, "locked_until": locked_until}
                )
                error = "邮箱或密码不正确"
                audit(
                    "auth.login_failed",
                    actor=email,
                    detail={"fails": count},
                    ip=client_ip(request),
                )
            elif not user.is_active:
                error = "账号已停用，请联系管理员"
            else:
                login(request, user)
                LoginThrottle.objects.filter(email=email).delete()
                audit(
                    "auth.login",
                    tenant_id=user.tenant_id_value,
                    actor=user.email,
                    ip=client_ip(request),
                    request_id=getattr(request, "request_id", ""),
                )
                from django.utils.http import url_has_allowed_host_and_scheme

                next_url = request.GET.get("next") or "/"
                # startswith("/") 会放过 "//evil.com"；必须做主机校验
                if not url_has_allowed_host_and_scheme(
                    next_url, {request.get_host()}, require_https=request.is_secure()
                ):
                    next_url = "/"
                return redirect(next_url)

    return render(request, "login.html", {"error": error, "email": email})


def logout_view(request):
    if request.user.is_authenticated:
        audit("auth.logout", tenant_id=request.user.tenant_id_value, actor=request.user.email)
    logout(request)
    return redirect(reverse("login"))
