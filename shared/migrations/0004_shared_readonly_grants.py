"""共享层只读授权：把「快照不可变」从一句口号变成数据库权限。

为什么必须单独一个迁移：
``ALTER DEFAULT PRIVILEGES`` 只在**建表那一刻**生效，而且按建表时所在的 schema 记账。
我们的表是先建在 ``app``（search_path 第一位）再 ``SET SCHEMA shared`` 搬过去的，
于是 app_rw 拿到的仍是 ``app`` 那套「SELECT/INSERT/UPDATE/DELETE」，
搬到 shared 后 UPDATE 权限跟着表走 —— 快照其实可以被 web 进程改掉。

这个问题只有用非超级用户真连一次才看得见（超级用户绕过一切权限检查），
所以它是由 ``scripts/verify_pg_rls.py`` 的 ⑦ 号检查暴露出来的。
"""

from django.db import migrations


def _role_exists(cur, role: str) -> bool:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
    return cur.fetchone() is not None


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        if not _role_exists(cur, "app_rw"):
            return
        # 共享层：只读 + 只插入，历史快照不可改、不可删
        cur.execute("REVOKE UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA shared FROM app_rw")
        cur.execute("GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA shared TO app_rw")
        cur.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA shared TO app_rw")
        # 以后新搬进来的表同样锁成只读（对 postgres 建的 shared 表生效）
        cur.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA shared "
            "REVOKE UPDATE, DELETE, TRUNCATE ON TABLES FROM app_rw"
        )


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        if not _role_exists(cur, "app_rw"):
            return
        cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA shared TO app_rw")
        cur.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA shared "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("shared", "0003_target_http_etag_target_http_last_modified_and_more"),
        ("tenancy", "0002_row_level_security"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
