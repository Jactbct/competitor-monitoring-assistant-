"""迁移里装 RLS 的公共实现。

新表必须自己装 RLS —— ``tenancy/0002`` 只对当时存在的表生效，后加的表默认没有 RLS，
隔离会在这里悄悄破口。把这段逻辑抽出来，避免每个迁移各写一份、写漏一处。
"""

from __future__ import annotations

_CANDIDATE_SCHEMAS = ("app", "public")


def actual_schema(cur, table: str) -> str | None:
    placeholders = ", ".join(["%s"] * len(_CANDIDATE_SCHEMAS))
    cur.execute(
        f"SELECT schemaname FROM pg_tables WHERE tablename = %s AND schemaname IN ({placeholders})",
        [table, *_CANDIDATE_SCHEMAS],
    )
    row = cur.fetchone()
    return row[0] if row else None


def enable_rls(schema_editor, table: str) -> None:
    """给一张表装 fail-closed 的行级安全策略（仅 PG）。"""
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        schema = actual_schema(cur, table)
        if schema is None:
            raise RuntimeError(f"{table} 不存在，无法启用 RLS —— 迁移顺序有问题")
        cur.execute(f'ALTER TABLE {schema}."{table}" ENABLE ROW LEVEL SECURITY')
        cur.execute(f'ALTER TABLE {schema}."{table}" FORCE ROW LEVEL SECURITY')
        cur.execute(f'DROP POLICY IF EXISTS tenant_isolation ON {schema}."{table}"')
        cur.execute(
            f"""CREATE POLICY tenant_isolation ON {schema}."{table}"
                USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"""
        )
        if schema != "app":
            cur.execute(f'ALTER TABLE {schema}."{table}" SET SCHEMA app')


def disable_rls(schema_editor, table: str) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        schema = actual_schema(cur, table)
        if schema is None:
            return
        cur.execute(f'ALTER TABLE {schema}."{table}" DISABLE ROW LEVEL SECURITY')
        cur.execute(f'DROP POLICY IF EXISTS tenant_isolation ON {schema}."{table}"')
