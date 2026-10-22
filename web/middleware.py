"""中间件：请求上下文（trace_id / request_id）与租户上下文。

租户上下文的顺序不可交换：
1. 从**会话**取 tenant（绝不从请求参数取）；
2. 参数里出现 ``tenant_id`` 直接 400 + 审计（这是越权探测的典型特征）；
3. 整个请求包在一个事务里，``SET LOCAL app.tenant_id`` 随事务结束自动清除
   —— 用 ``SET`` 会在连接池复用时串租户，是这类系统最经典的事故。
"""

from __future__ import annotations

import time
import uuid

from django.http import JsonResponse
from django.shortcuts import render

from core.config import get_settings
from core.db import tenant_atomic
from core.errors import AppError, ErrorCode, error_payload
from core.log import bind_trace, get_logger
from core.tenant import tenant_scope
from tenancy.services import audit

log = get_logger("web")


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = (request.headers.get("X-Request-Id") or uuid.uuid4().hex)[:32]
        request.request_id = request_id
        started = time.perf_counter()
        with bind_trace(request_id):
            response = self.get_response(request)
            duration = int((time.perf_counter() - started) * 1000)
            if request.path not in {"/health", "/favicon.ico"}:
                log.info(
                    "http.request",
                    extra={
                        "method": request.method,
                        "path": request.path,
                        "status": response.status_code,
                        "duration_ms": duration,
                        "tenant_id": getattr(request, "tenant_id", None),
                    },
                )
        response["X-Request-Id"] = request_id
        return response


class TenantContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        tenant_id = None
        if user is not None and user.is_authenticated and not user.is_platform_ops:
            tenant_id = user.tenant_id_value

        # 只在表单提交时解析 body：否则任意 POST（含 multipart 上传）都会被白读一遍
        in_post = (
            request.method == "POST"
            and request.content_type == "application/x-www-form-urlencoded"
            and "tenant_id" in request.POST
        )
        probe = "tenant_id" in request.GET or in_post
        if probe:
            audit(
                "tenant.cross_access_blocked",
                tenant_id=tenant_id,
                actor=getattr(user, "email", ""),
                detail={"path": request.path, "reason": "tenant_id in request params"},
                ip=_client_ip(request),
                request_id=getattr(request, "request_id", ""),
            )
            err = AppError.of(
                ErrorCode.TENANT_MISSING_CONTEXT, "租户上下文只能来自登录会话，不接受 tenant_id 参数"
            )
            return JsonResponse(error_payload(err, getattr(request, "request_id", "")), status=400)

        request.tenant_id = tenant_id
        with tenant_scope(tenant_id), tenant_atomic(tenant_id):
            return self.get_response(request)


def _client_ip(request) -> str:
    """只有明确配置了反向代理时才信 X-Forwarded-For，否则审计里的 IP 可被伪造。"""
    if get_settings().trusted_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
    return (request.META.get("REMOTE_ADDR") or "")[:45]


class ErrorHandlingMiddleware:
    """把 :class:`AppError` 转成统一错误结构；HTMX 请求回 JSON，页面请求回错误页。

    注意：**必须走 ``process_exception``，不能在 ``__call__`` 里 try/except**。
    Django 给每个中间件的 ``get_response`` 已经套了 ``convert_exception_to_response``，
    视图抛的异常在到达中间件之前就被转成了 500 响应，``__call__`` 里永远 except 不到。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if not isinstance(exception, AppError):
            return None
        request_id = getattr(request, "request_id", "")
        log.warning(
            "app.error",
            extra={"code": exception.code, "status": exception.http_status, "path": request.path},
        )
        if request.headers.get("HX-Request") or request.path.startswith("/api/"):
            return JsonResponse(error_payload(exception, request_id), status=exception.http_status)
        return render(
            request,
            "error.html",
            {"error": exception, "request_id": request_id},
            status=exception.http_status,
        )
