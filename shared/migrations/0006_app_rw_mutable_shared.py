"""把「需求投影」的可写权限精确授给 app_rw。

背景：`add_target / set_priority / delete_binding` 都会调 `recompute_demand()`，而它要写
`shared.target.interval_min` 与 `shared.target_demand`。之前为了「快照不可变」把整个
`shared` schema 对 app_rw 设成只读，结果是**生产环境添加监控对象 100% 失败**
（SQLite 开发库没权限概念，所以本地一直是对的）。

修法不是放开整个 shared 层，而是只放开这两张「可变投影表」：
快照、事件、任务表仍然只读/只插。这道边界值得用列级授权进一步收紧，
但列级 GRANT 会让迁移与后续加列都要同步改权限，收益不抵成本，故停在表级。
"""

from django.db import migrations

_GRANT = "GRANT UPDATE ON shared.target, shared.target_demand TO app_rw"
_REVOKE = "REVOKE UPDATE ON shared.target, shared.target_demand FROM app_rw"


def _role_exists(cur, role: str = "app_rw") -> bool:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
    return cur.fetchone() is not None


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        if _role_exists(cur):
            cur.execute(_GRANT)


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        if _role_exists(cur):
            cur.execute(_REVOKE)


class Migration(migrations.Migration):
    dependencies = [
        ("shared", "0005_brin_indexes"),
        ("tenancy", "0003_llmusage"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
