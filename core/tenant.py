"""租户上下文（应用层防线，补充文档 §8.1 三道防线的第 ② 层）。

* 租户 id 只从登录会话取，**任何接口不接受 tenant 参数**。
* ORM 默认管理器按当前上下文自动过滤；未设置上下文时过滤到 ``tenant_id IS NULL``
  → 租户业务表返回 0 行（fail-closed，与 RLS 行为一致）。
* worker / 平台运营等需要跨租户的场景显式用 ``all_objects``，并留审计痕迹。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

__all__ = ["current_tenant_id", "tenant_scope", "TenantScopedManager", "TenantScopedQuerySet"]

_tenant_id: ContextVar[str | None] = ContextVar("tenant_id", default=None)

#: 未设置上下文时用于查询的哨兵值（保证生成合法 UUID 且必定查不到数据）
_NO_TENANT = "00000000-0000-0000-0000-000000000000"


def current_tenant_id() -> str | None:
    return _tenant_id.get()


@contextmanager
def tenant_scope(tenant_id: str | None) -> Iterator[str | None]:
    token = _tenant_id.set(tenant_id)
    try:
        yield tenant_id
    finally:
        _tenant_id.reset(token)


def bound_tenant_id() -> str:
    return _tenant_id.get() or _NO_TENANT


def _make_manager() -> type:
    """延迟 import：本模块要能被 domain 之外的任意层引用，但不想在 import 期碰 Django app registry。"""
    from django.db import models

    class TenantScopedQuerySet(models.QuerySet):
        def for_tenant(self, tenant_id: str) -> Any:
            return self.filter(tenant_id=tenant_id)

    class TenantScopedManager(models.Manager.from_queryset(TenantScopedQuerySet)):
        """默认管理器：自动带上当前租户过滤。"""

        def get_queryset(self) -> Any:
            return super().get_queryset().filter(tenant_id=bound_tenant_id())

    return TenantScopedManager


TenantScopedManager = _make_manager()
TenantScopedQuerySet = TenantScopedManager._queryset_class  # type: ignore[attr-defined]
