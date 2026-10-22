"""生产环境（PostgreSQL）双 schema 布局：把 0001 建出来的表搬进 shared / app。

为什么单独一个迁移：Django 的 ``db_table`` 会写死进 migration 文件，
如果按后端拼 ``shared.snapshot`` 这种限定名，SQLite 开发库与 PG 生产库会生成
两套不同的迁移，迟早打架。所以模型只写纯表名（表名在两个 schema 里全局唯一），
连接用 ``search_path=app,shared,public`` 解析，本迁移负责一次性归位。

搬移是**按当前所在 schema 实际查找**的，而不是假设都在 public：
因为 ``deploy/init/01_roles.sql`` 会先建好两个 schema，此时建表会落到 search_path
第一位（app）。两种初始化顺序都必须能跑通。

⚠️ 以后**新增 shared 层表**时，必须同步把表名加进 ``SHARED_TABLES`` 并补一条搬移。
    新增 app 层表不需要处理：``search_path`` 的第一位就是 ``app``。
"""

from django.db import migrations

SHARED_TABLES = (
    "target",
    "target_demand",
    "snapshot",
    "change_event",
    "llm_cache",
    "job",
    "rate_bucket",
)

APP_TABLES = (
    "tenant",
    "app_user",
    "self_product",
    "tenant_target",
    "event_priority",
    "analysis",
    "suggestion",
    "task",
    "feedback",
    "notification",
    "audit_log",
    "metric_daily",
    "login_throttle",
    "analytics_event",
)

_CANDIDATE_SCHEMAS = ("public", "app", "shared", "django")


def _move(cursor, table: str, target_schema: str) -> None:
    cursor.execute(
        "SELECT schemaname FROM pg_tables WHERE tablename = %s AND schemaname = ANY(%s)",
        [table, list(_CANDIDATE_SCHEMAS)],
    )
    row = cursor.fetchone()
    if row is None:
        return
    current = row[0]
    if current == target_schema:
        return
    cursor.execute(f'ALTER TABLE "{current}"."{table}" SET SCHEMA {target_schema}')


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS shared")
        cur.execute("CREATE SCHEMA IF NOT EXISTS app")
        for table in SHARED_TABLES:
            _move(cur, table, "shared")
        for table in APP_TABLES:
            _move(cur, table, "app")


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        for table in SHARED_TABLES:
            _move(cur, table, "public")
        for table in APP_TABLES:
            _move(cur, table, "public")


class Migration(migrations.Migration):
    dependencies = [("shared", "0001_initial")]
    operations = [migrations.RunPython(forwards, backwards)]
