"""快照表的 BRIN 索引 + 保留期相关的数据库侧准备（仅 PostgreSQL）。

为什么用 BRIN 而不是 B-tree：``captured_at`` 是**单调递增**的写入时间，
物理存储顺序与时间高度相关 —— 这正是 BRIN 的最佳场景。
BRIN 索引体积通常只有 B-tree 的百分之一，而范围扫描（按时间清理、按时间查时间轴）
一样快。快照表最终会有千万行，索引体积就是实打实的成本。

SQLite 没有 BRIN，直接跳过（开发/演示量级用普通索引足够）。
"""

from django.db import migrations


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        # pages_per_range=32：默认 128 太粗，快照写入密集，细一点更适合范围扫描
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_snapshot_captured_brin "
            "ON shared.snapshot USING brin (captured_at) WITH (pages_per_range = 32)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_event_detected_brin "
            "ON shared.change_event USING brin (detected_at) WITH (pages_per_range = 32)"
        )


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS shared.ix_snapshot_captured_brin")
        cur.execute("DROP INDEX IF EXISTS shared.ix_event_detected_brin")


class Migration(migrations.Migration):
    dependencies = [("shared", "0004_shared_readonly_grants")]
    operations = [migrations.RunPython(forwards, backwards)]
