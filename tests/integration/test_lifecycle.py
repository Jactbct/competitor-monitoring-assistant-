"""数据生命周期：保留期清理、体积统计、BRIN 索引。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from shared.maintenance import purge_snapshots, snapshot_volume, table_stats
from shared.models import ChangeEvent, Snapshot


def _snapshot(target, captured_at, evidence_id="snap-old"):
    return Snapshot.objects.create(
        target=target,
        captured_at=captured_at,
        http_status=200,
        content_sha256="c" * 64,
        fields={"title": "旧数据"},
        field_completeness="1.000",
        price_display="99.00",
        price_final="99.00",
        price_confidence="certain",
        evidence_id=evidence_id,
        status=Snapshot.STATUS_VALID,
    )


def test_purge_removes_only_expired_and_deletes_events_first(target):
    old = _snapshot(target, timezone.now() - timedelta(days=500), "snap-old-1")
    fresh = _snapshot(target, timezone.now() - timedelta(days=10), "snap-fresh-1")
    ChangeEvent.objects.create(
        target=target,
        snapshot=old,
        prev_snapshot_id=None,
        changed_fields=["price_final"],
        event_types=["real_price"],
        deltas={},
        detected_at=timezone.now() - timedelta(days=500),
    )

    result = purge_snapshots(400)

    assert result["snapshots"] == 1
    assert result["events"] == 1
    assert not Snapshot.objects.filter(pk=old.pk).exists()
    assert Snapshot.objects.filter(pk=fresh.pk).exists()
    assert ChangeEvent.objects.count() == 0


def test_purge_is_safe_when_nothing_to_delete(target):
    _snapshot(target, timezone.now() - timedelta(days=1), "snap-new-1")
    result = purge_snapshots(400)
    assert result["snapshots"] == 0
    assert result["events"] == 0
    assert Snapshot.objects.count() == 1


def test_purge_respects_batch_size(target):
    for i in range(5):
        _snapshot(target, timezone.now() - timedelta(days=500 + i), f"snap-batch-{i}")
    result = purge_snapshots(400, batch_size=2, max_batches=10)
    assert result["snapshots"] == 5
    assert Snapshot.objects.count() == 0


def test_snapshot_volume_reports_rows_and_range(target):
    _snapshot(target, timezone.now() - timedelta(days=2), "snap-vol-1")
    volume = snapshot_volume()
    assert volume["rows"] == 1
    assert volume["oldest"] is not None
    assert volume["targets"] == 1


def test_table_stats_returns_rows_for_known_tables(target):
    _snapshot(target, timezone.now(), "snap-stats-1")
    rows = table_stats()
    assert rows, "至少应返回快照表的信息"
    assert any(row["table"] == "shared.snapshot" for row in rows)
    assert all(row["total_bytes"] >= 0 for row in rows)


def test_table_stats_on_pg_reports_real_bytes(target):
    if connection.vendor != "postgresql":
        pytest.skip("仅 PG 有体积信息")
    _snapshot(target, timezone.now(), "snap-bytes-1")
    rows = {row["table"]: row for row in table_stats()}
    assert rows["shared.snapshot"]["total_bytes"] > 0


def test_brin_index_exists_on_pg(target):
    """BRIN 是千万元级快照表的体积救星，必须真的建上（SQLite 无此概念）。"""
    if connection.vendor != "postgresql":
        pytest.skip("BRIN 仅 PG")
    with connection.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            " WHERE schemaname = 'shared' AND indexname = 'ix_snapshot_captured_brin'"
        )
        row = cur.fetchone()
    assert row is not None
    assert "brin" in row[0].lower()
