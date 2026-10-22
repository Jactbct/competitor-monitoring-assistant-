"""共享层数据生命周期：快照/事件的保留期清理与体积观测。

为什么需要这个模块：快照是**只增不改**的表，每采集一次落一行，30% 变化率下
一天约 2.6 万行、一年近千万行。没有保留期就是「缓慢必然」的磁盘事故。

为什么先做保留期、不先做分区：见 `docs/adr/005-snapshot-partitioning.md` ——
本项目当前量级（1 QPS、单类目）下，单表 + BRIN 索引 + 400 天保留完全够用；
分区会引入「Django 模型需要复合主键 / 迁移要手写 DDL」的复杂度，
在触发阈值（>2000 万行 或 单表 >50 GB）出现前不该提前付这笔成本。
"""

from __future__ import annotations

from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Count
from django.utils import timezone

from core.db import is_postgres
from core.log import get_logger
from shared.models import ChangeEvent, Snapshot, Target

__all__ = ["purge_snapshots", "snapshot_volume", "table_stats"]

log = get_logger("maintenance")


def purge_snapshots(
    retention_days: int, *, batch_size: int = 5000, max_batches: int = 200
) -> dict:
    """按保留期删除快照与变化事件（**先删事件，再删快照**）。

    注意：``ChangeEvent.snapshot`` 是 ``DO_NOTHING`` 外键，直接删快照会撞外键约束。
    这也是一处设计取舍：我们宁可显式按顺序删，也不让数据库级联删掉事件 ——
    级联删除的 bug 一旦发生，丢的是别人的历史数据。

    租户侧的建议/任务/反馈**不删**：那是人的工作记录，且卡片里已存了证据引文文本；
    快照过期后证据 ID 不再能查回快照详情，但结论与引文仍然可读（400 天的窗口足够长）。
    """
    cutoff = timezone.now() - timedelta(days=retention_days)
    events_deleted = 0
    snapshots_deleted = 0

    for _ in range(max_batches):
        with transaction.atomic():
            ids = list(
                Snapshot.objects.filter(captured_at__lt=cutoff)
                .order_by("captured_at")
                .values_list("id", flat=True)[:batch_size]
            )
            if not ids:
                break
            events_deleted += ChangeEvent.objects.filter(snapshot_id__in=ids).delete()[0]
            snapshots_deleted += Snapshot.objects.filter(id__in=ids).delete()[0]
        if len(ids) < batch_size:
            break

    if snapshots_deleted or events_deleted:
        log.info(
            "maintenance.purged",
            extra={
                "snapshot_deleted": snapshots_deleted,
                "event_deleted": events_deleted,
                "retention_days": retention_days,
            },
        )
    return {"snapshots": snapshots_deleted, "events": events_deleted, "cutoff": cutoff.isoformat()}


def snapshot_volume() -> dict:
    """快照行数与最早/最新时间，供 ops 页判断「还要不要分区」。"""
    agg = Snapshot.objects.aggregate(total=Count("id"))
    oldest = Snapshot.objects.order_by("captured_at").values_list("captured_at", flat=True).first()
    newest = Snapshot.objects.order_by("-captured_at").values_list("captured_at", flat=True).first()
    return {
        "rows": agg["total"] or 0,
        "oldest": oldest.isoformat() if oldest else None,
        "newest": newest.isoformat() if newest else None,
        "targets": Target.objects.count(),
    }


_TABLES = (
    ("shared", "snapshot"),
    ("shared", "change_event"),
    ("shared", "job"),
    ("app", "suggestion"),
    ("app", "audit_log"),
    ("app", "analysis"),
)


def table_stats() -> list[dict]:
    """表体积（仅 PG；SQLite 只给行数）。用于回答「现在要不要分区」。"""
    rows: list[dict] = []
    if is_postgres():
        with connection.cursor() as cur:
            for schema, table in _TABLES:
                cur.execute(
                    "SELECT pg_total_relation_size(c.oid), pg_indexes_size(c.oid), "
                    "       c.reltuples::bigint "
                    "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    " WHERE n.nspname = %s AND c.relname = %s",
                    [schema, table],
                )
                fetched = cur.fetchone()
                if not fetched:
                    continue
                total, index_size, estimate = fetched
                # reltuples 在「从未 analyze 过」的表上返回 -1，直接显示会变成 ≈-1
                estimated_rows = int(estimate) if estimate is not None and estimate > 0 else 0
                rows.append(
                    {
                        "table": f"{schema}.{table}",
                        "total_bytes": int(total or 0),
                        "index_bytes": int(index_size or 0),
                        "rows": estimated_rows,
                        "exact": False,
                        "unknown": estimate is None or estimate < 0,
                    }
                )
        return rows

    counts = {
        "shared.snapshot": Snapshot.objects.count(),
        "shared.change_event": ChangeEvent.objects.count(),
    }
    for table, count in counts.items():
        rows.append({"table": table, "total_bytes": 0, "index_bytes": 0, "rows": count, "exact": True})
    return rows
