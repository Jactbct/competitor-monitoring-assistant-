"""错误码体系（架构文档 §5.2 / 技术方案补充 §5.1）。

约定：``领域_原因``，HTTP 状态由错误码决定，客户端只看到 code/message/details，
永远不出现堆栈。跨租户访问统一返回 404（不暴露资源存在性）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["AppError", "ErrorCode", "error_payload"]


class ErrorCode:
    COMMON_VALIDATION_FAILED = ("COMMON_VALIDATION_FAILED", 400)
    COMMON_NOT_FOUND = ("COMMON_NOT_FOUND", 404)
    COMMON_RATE_LIMITED = ("COMMON_RATE_LIMITED", 429)

    AUTH_REQUIRED = ("AUTH_REQUIRED", 401)
    AUTH_INVALID_CREDENTIALS = ("AUTH_INVALID_CREDENTIALS", 401)
    AUTH_ACCOUNT_LOCKED = ("AUTH_ACCOUNT_LOCKED", 423)
    AUTH_ROLE_FORBIDDEN = ("AUTH_ROLE_FORBIDDEN", 403)

    TENANT_MISSING_CONTEXT = ("TENANT_MISSING_CONTEXT", 400)
    TENANT_CROSS_ACCESS_BLOCKED = ("TENANT_CROSS_ACCESS_BLOCKED", 404)

    TARGET_NOT_FOUND = ("TARGET_NOT_FOUND", 404)
    TARGET_DUPLICATED = ("TARGET_DUPLICATED", 409)
    TARGET_QUOTA_EXCEEDED = ("TARGET_QUOTA_EXCEEDED", 409)
    TARGET_INVALID_URL = ("TARGET_INVALID_URL", 400)

    QUOTA_COLLECT_EXCEEDED = ("QUOTA_COLLECT_EXCEEDED", 429)

    COLLECT_CIRCUIT_OPEN = ("COLLECT_CIRCUIT_OPEN", 503)
    COLLECT_ABNORMAL_PAGE = ("COLLECT_ABNORMAL_PAGE", 502)

    ANALYZE_DEGRADED = ("ANALYZE_DEGRADED", 200)
    ANALYZE_SCHEMA_INVALID = ("ANALYZE_SCHEMA_INVALID", 500)

    SUGGEST_COST_MISSING = ("SUGGEST_COST_MISSING", 422)
    SUGGEST_NOT_FOUND = ("SUGGEST_NOT_FOUND", 404)
    SUGGEST_COMPLIANCE_BLOCKED = ("SUGGEST_COMPLIANCE_BLOCKED", 422)


@dataclass
class AppError(Exception):
    code: str
    message: str
    http_status: int = 400
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    @classmethod
    def of(cls, pair: tuple[str, int], message: str, **details: Any) -> AppError:
        code, status = pair
        return cls(code=code, message=message, http_status=status, details=details)


def error_payload(err: AppError, request_id: str = "") -> dict[str, Any]:
    return {
        "error": {
            "code": err.code,
            "message": err.message,
            "request_id": request_id,
            "details": err.details,
        }
    }
