"""数据库访问的唯一入口（架构文档 §5.2「只有 core/db.py 与 models 能碰 ORM 会话」）。

两个后端：
* PostgreSQL（生产）：两个真实 schema（``shared`` / ``app``）+ RLS + ``SKIP LOCKED``。
* SQLite（开发/演示）：表名加前缀模拟分层（``shared_target`` / ``app_suggestion``），
  租户隔离由 ORM 默认管理器（:mod:`core.tenant`）承担。**这不是生产形态**，
  生产必须 PG，否则 RLS 这道防线不存在。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from django.db import connection, transaction

__all__ = [
    "APP_SCHEMA",
    "SHARED_SCHEMA",
    "SHARED_TABLES",
    "is_postgres",
    "quote_table",
    "set_tenant_context",
    "table_exists",
    "tenant_atomic",
    "use_sqlite",
]

SHARED_SCHEMA = "shared"
APP_SCHEMA = "app"

#: 共享层表名（生产环境迁入 ``shared`` schema；开发库平铺）
SHARED_TABLES = frozenset(
    {"target", "target_demand", "snapshot", "change_event", "llm_cache", "job", "rate_bucket"}
)


def is_postgres() -> bool:
    return connection.vendor == "postgresql"


def use_sqlite() -> bool:
    return connection.vendor == "sqlite"


def quote_table(schema: str, table: str) -> str:
    """模型 ``db_table``。

    两张 schema 里的表名**全局唯一**，所以模型只写纯表名，生产环境的 PG 连接用
    ``search_path=app,shared,public`` 定位到正确 schema（见 settings 的 OPTIONS）。

    这样做的原因：``db_table`` 会被写进 migration 文件，若按后端拼 ``shared.target``
    这类限定名，SQLite 开发库与 PG 生产库会生成两套不同的迁移，迟早打架。
    schema 的真实归属由 ``shared/migrations/0002_pg_schema_layout.py`` 一次性搬移。
    """
    return table


def set_tenant_context(tenant_id: str | None) -> None:
    """在**当前事务**内设置租户上下文。

    必须用 ``SET LOCAL``：``SET`` 是会话级，连接池复用时租户上下文会串
    —— 这类系统最经典的事故（补充文档 §8.1）。
    """
    if not tenant_id:
        return
    if not is_postgres():
        # SQLite 无会话变量；隔离靠 ORM 管理器。这里显式留痕，避免误以为有 RLS。
        return
    with connection.cursor() as cur:
        cur.execute("SET LOCAL app.tenant_id = %s", [str(tenant_id)])


def current_tenant_setting() -> str | None:
    """读当前事务里的 ``app.tenant_id``（未设置为 None）。"""
    if not is_postgres():
        return None
    with connection.cursor() as cur:
        cur.execute("SELECT NULLIF(current_setting('app.tenant_id', true), '')")
        row = cur.fetchone()
    return row[0] if row else None


@contextlib.contextmanager
def tenant_atomic(tenant_id: str | None) -> Iterator[None]:
    """一个请求/一个作业 = 一个事务 + 一次 ``SET LOCAL``。

    ``SET LOCAL`` 只在**事务**结束时回滚，**不会在嵌套 savepoint 释放时回滚**。
    所以嵌套调用（比如在租户 A 的请求里写租户 B 的审计）必须显式恢复外层值，
    否则后续所有查询都会被 RLS 过滤成 B 的数据。
    """
    with transaction.atomic():
        previous = current_tenant_setting()
        set_tenant_context(tenant_id)
        try:
            yield
        finally:
            if previous != (str(tenant_id) if tenant_id else None):
                set_tenant_context(previous)


def table_exists(table: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s" if is_postgres() else
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = %s",
            [table],
        )
        return cur.fetchone() is not None


def raw_fetchall(sql: str, params: Any = None) -> list[tuple]:
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchall()
