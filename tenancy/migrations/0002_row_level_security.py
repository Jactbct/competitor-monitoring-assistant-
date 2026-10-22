"""租户层行级安全（RLS）—— 隔离的最后一道防线。

三条硬规则（架构文档 §5.4）：
1. ``ENABLE`` + ``FORCE`` ROW LEVEL SECURITY：不加 FORCE，表 owner 直接绕过，
   而应用通常就用 owner 连库 —— RLS 等于没开。
2. 策略用 ``current_setting('app.tenant_id', true)``：带 ``true`` 时未设置上下文返回 NULL，
   ``NULL = ...`` 为 UNKNOWN → 策略不通过 → **全表拒绝**（fail-closed）。
   不带 ``true`` 会抛异常，运维顺手 catch 掉就变成 fail-open。
   外面再套一层 ``NULLIF(..., '')``：``SET LOCAL`` 在事务结束后可能留下空字符串，
   直接 ``''::uuid`` 会抛 22P02，把「拒绝」变成「500」。
3. 例外：``tenant`` / ``app_user`` / ``login_throttle`` 不开 RLS ——
   登录必须在知道租户之前按 email 查用户，这三张表的访问由服务层限制。

worker 需要一个 ``BYPASSRLS`` 的账号（``app_worker``）才能跨租户扇出与汇总；
web 用 ``app_rw``（受 RLS 约束）。见 ``deploy/init/01_roles.sql``。
"""

from django.db import migrations

RLS_TABLES = (
    "self_product",
    "tenant_target",
    "event_priority",
    "analysis",
    "suggestion",
    "task",
    "feedback",
    "notification",
    "audit_log",
    "analytics_event",
)

#: 刻意不开 RLS 的表，理由必须写清楚（否则下次有人「顺手补上」就出事）：
#:
#: * ``tenant`` / ``app_user`` / ``login_throttle`` —— 登录必须在知道租户之前按 email 查用户；
#: * ``metric_daily`` —— **只放无租户维度的运维指标**（快照数、队列深度、异常率）。
#:   按租户的模型用量/成本在 ``llm_usage`` 表里，那张表是开 RLS 的。
NO_RLS_TABLES = ("tenant", "app_user", "login_throttle", "metric_daily")


def _policy_sql(table: str) -> list[str]:
    return [
        f'ALTER TABLE app."{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE app."{table}" FORCE ROW LEVEL SECURITY',
        f'DROP POLICY IF EXISTS tenant_isolation ON app."{table}"',
        f"""CREATE POLICY tenant_isolation ON app."{table}"
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""",
    ]


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        for table in RLS_TABLES:
            for statement in _policy_sql(table):
                cur.execute(statement)
        # 共享层硬规则：不得出现任何租户标识列（用注释固化，CI 另有断言）
        cur.execute(
            """
            COMMENT ON SCHEMA shared IS
            'L1 公开快照层：任何表不得含 tenant_id/user_id/owner_id/creator_id'
            """
        )


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        for table in RLS_TABLES:
            cur.execute(f'ALTER TABLE app."{table}" DISABLE ROW LEVEL SECURITY')
            cur.execute(f'DROP POLICY IF EXISTS tenant_isolation ON app."{table}"')


class Migration(migrations.Migration):
    dependencies = [
        ("tenancy", "0001_initial"),
        ("shared", "0002_pg_schema_layout"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
