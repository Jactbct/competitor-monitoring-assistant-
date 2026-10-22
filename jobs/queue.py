"""任务表 = 队列（架构文档 §6.4：PG + ``SKIP LOCKED``，不引入 Redis / Celery）。

为什么值得单独说一句：任务就是一张表，排障时 ``SELECT * FROM shared_job WHERE state='failed'``
就能看到全貌；进程重启任务不丢；延迟任务只是 ``run_after`` 的一个 WHERE 条件。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.utils import timezone

from core.db import is_postgres
from shared.models import Job

__all__ = [
    "claim",
    "complete",
    "enqueue",
    "fail",
    "purge_finished",
    "queue_stats",
    "reclaim_stale",
    "retry_delay",
]

_BACKOFF_SECONDS = (1, 5, 25)

#: 各类型任务的租约时长（秒）。宁可给长，也不要误回收导致重复副作用。
LEASE_SECONDS = {
    "schedule_tick": 600,
    "collect": 300,
    "analyze": 900,
    "notify": 300,
    "health_snapshot": 900,
    "silence_check": 300,
    "daily_digest": 3600,
}


def lease_for(kind: str) -> int:
    return LEASE_SECONDS.get(kind, 600)


def retry_delay(attempts: int) -> timedelta:
    index = min(max(attempts - 1, 0), len(_BACKOFF_SECONDS) - 1)
    return timedelta(seconds=_BACKOFF_SECONDS[index])


def enqueue(
    kind: str,
    payload: dict[str, Any],
    *,
    run_after: datetime | None = None,
    priority: int = 100,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
) -> Job | None:
    """投递任务。带幂等键时重复投递返回既有任务（不新增）。"""
    if idempotency_key:
        existing = Job.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return None
    try:
        # 并发下 check-then-create 会撞唯一约束；撞了就说明别人已经投过，按幂等返回 None。
        # 用 savepoint 保证只回滚这一条语句，不污染调用方的事务。
        with transaction.atomic():
            return Job.objects.create(
                kind=kind,
                payload=payload,
                run_after=run_after or timezone.now(),
                priority=priority,
                idempotency_key=idempotency_key,
                max_attempts=max_attempts,
            )
    except IntegrityError:
        return None


def claim(limit: int, worker_id: str) -> list[Job]:
    """取一批到期任务并标记 running。PG 用 SKIP LOCKED，SQLite 用乐观 Update 兜住。"""
    now = timezone.now()
    with transaction.atomic():
        if is_postgres():
            with connection.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id FROM {Job._meta.db_table}
                     WHERE state = 'pending' AND run_after <= %s
                     ORDER BY priority ASC, run_after ASC
                     LIMIT %s
                     FOR UPDATE SKIP LOCKED
                    """,
                    [now, limit],
                )
                ids = [row[0] for row in cur.fetchall()]
        else:
            ids = list(
                Job.objects.filter(state=Job.STATE_PENDING, run_after__lte=now)
                .order_by("priority", "run_after")
                .values_list("id", flat=True)[:limit]
            )

        if not ids:
            return []

        # 只有仍是 pending 的行会被抢到（乐观锁，多进程下不会重复执行）
        claimed = list(
            Job.objects.filter(id__in=ids, state=Job.STATE_PENDING).select_for_update(skip_locked=is_postgres())
        )
        for job in claimed:
            job.state = Job.STATE_RUNNING
            job.locked_by = worker_id
            job.locked_at = now
            job.lease_until = now + timedelta(seconds=lease_for(job.kind))
            job.attempts = F("attempts") + 1
        Job.objects.bulk_update(
            claimed, ["state", "locked_by", "locked_at", "lease_until", "attempts"]
        )
    for job in claimed:
        job.refresh_from_db(fields=["attempts"])
    return claimed


def complete(job: Job) -> None:
    Job.objects.filter(pk=job.pk).update(
        state=Job.STATE_DONE,
        finished_at=timezone.now(),
        last_error=None,
        locked_by=None,
        lease_until=None,
    )


def fail(job: Job, error: str, *, now: datetime | None = None) -> str:
    """失败：未超次数则退避重试，超了进 dead（不静默丢弃）。"""
    now = now or timezone.now()
    attempts = job.attempts or 1
    if attempts >= (job.max_attempts or 3):
        Job.objects.filter(pk=job.pk).update(
            state=Job.STATE_DEAD,
            last_error=error[:1000],
            finished_at=now,
            locked_by=None,
            lease_until=None,
        )
        return Job.STATE_DEAD
    Job.objects.filter(pk=job.pk).update(
        state=Job.STATE_PENDING,
        last_error=error[:1000],
        run_after=now + retry_delay(attempts),
        locked_by=None,
        locked_at=None,
        lease_until=None,
    )
    return Job.STATE_PENDING


def reclaim_stale(older_than_seconds: int = 300) -> int:
    """回收「进程被杀」导致卡在 running 的任务。

    有租约的任务按 ``lease_until`` 判断（长任务不会被误杀），没有租约的老数据回退到 locked_at。
    """
    now = timezone.now()
    expired_lease = Job.objects.filter(state=Job.STATE_RUNNING, lease_until__lt=now)
    legacy = Job.objects.filter(
        state=Job.STATE_RUNNING,
        lease_until__isnull=True,
        locked_at__lt=now - timedelta(seconds=older_than_seconds),
    )
    return expired_lease.update(
        state=Job.STATE_PENDING, locked_by=None, locked_at=None, lease_until=None
    ) + legacy.update(state=Job.STATE_PENDING, locked_by=None, locked_at=None)


def purge_finished(days: int = 30) -> int:
    """清理历史任务。默认 30 天而不是 7 天：删掉 done 行会**释放幂等键**，
    7 天窗口太容易让同一件通知/同一轮 tick 被重新投递。"""
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = Job.objects.filter(state__in=[Job.STATE_DONE, Job.STATE_DEAD], finished_at__lt=cutoff).delete()
    return deleted


def queue_stats() -> dict:
    now = timezone.now()
    pending = Job.objects.filter(state=Job.STATE_PENDING).count()
    backlog = Job.objects.filter(state=Job.STATE_PENDING, run_after__lt=now - timedelta(minutes=5)).count()
    running = Job.objects.filter(state=Job.STATE_RUNNING).count()
    dead = Job.objects.filter(state=Job.STATE_DEAD).count()
    return {
        "pending": pending,
        "running": running,
        # 注意：没有 failed 状态 —— fail() 要么退回 pending，要么进 dead。
        # 这里以前统计了一个永远不会出现的状态，看板上的 0 是假的。
        "dead": dead,
        "backlog_5m": backlog,
    }


def new_worker_id() -> str:
    return f"worker-{uuid.uuid4().hex[:8]}"
