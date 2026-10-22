"""PG 任务表作为队列的行为：幂等、抢占、退避、死信、预算分批。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from infra import budget
from jobs.queue import claim, complete, enqueue, fail, purge_finished, queue_stats, reclaim_stale, retry_delay
from shared.models import Job

pytestmark = pytest.mark.django_db


def test_enqueue_is_idempotent_with_key():
    first = enqueue("analyze", {"x": 1}, idempotency_key="k1")
    second = enqueue("analyze", {"x": 1}, idempotency_key="k1")
    assert first is not None
    assert second is None
    assert Job.objects.filter(idempotency_key="k1").count() == 1


def test_claim_marks_running_and_counts_attempt():
    enqueue("collect", {"target_id": 1})
    jobs = claim(10, "w1")
    assert len(jobs) == 1
    assert jobs[0].state == Job.STATE_RUNNING
    assert jobs[0].attempts == 1
    assert claim(10, "w2") == []


def test_claim_respects_run_after():
    enqueue("collect", {"target_id": 1}, run_after=timezone.now() + timedelta(minutes=5))
    assert claim(10, "w1") == []


def test_complete_marks_done():
    enqueue("collect", {})
    job = claim(1, "w1")[0]
    complete(job)
    job.refresh_from_db()
    assert job.state == Job.STATE_DONE
    assert job.finished_at is not None


def test_fail_retries_with_backoff_then_dies():
    enqueue("collect", {})
    job = claim(1, "w1")[0]

    state = fail(job, "boom")
    job.refresh_from_db()
    assert state == Job.STATE_PENDING
    assert job.run_after > timezone.now()
    assert job.attempts == 1

    job.state = Job.STATE_RUNNING
    job.attempts = 3
    job.save()
    state = fail(job, "boom again")
    job.refresh_from_db()
    assert state == Job.STATE_DEAD
    assert "boom again" in job.last_error


def test_retry_delay_grows_then_caps():
    assert retry_delay(1).seconds == 1
    assert retry_delay(2).seconds == 5
    assert retry_delay(3).seconds == 25
    assert retry_delay(9).seconds == 25


def test_stale_running_job_is_reclaimed():
    """租约到期才回收：只改 locked_at 不再有效（防止长任务被误杀）。"""
    enqueue("collect", {})
    job = claim(1, "w1")[0]
    Job.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))

    assert reclaim_stale(300) == 1
    job.refresh_from_db()
    assert job.state == Job.STATE_PENDING
    assert job.lease_until is None


def test_long_job_within_lease_is_not_reclaimed():
    """日报这类长任务在租约内绝不能被回收，否则会重复发日报、重复清理。"""
    enqueue("daily_digest", {})
    job = claim(1, "w1")[0]
    job.refresh_from_db()
    assert job.lease_until > timezone.now() + timedelta(minutes=30)

    assert reclaim_stale(1) == 0
    job.refresh_from_db()
    assert job.state == Job.STATE_RUNNING


def test_queue_stats_reports_backlog():
    enqueue("collect", {}, run_after=timezone.now() - timedelta(minutes=10))
    stats = queue_stats()
    assert stats["pending"] == 1
    assert stats["backlog_5m"] == 1


def test_purge_finished_removes_old_done():
    enqueue("collect", {}, idempotency_key="old")
    job = claim(1, "w1")[0]
    complete(job)
    Job.objects.filter(pk=job.pk).update(finished_at=timezone.now() - timedelta(days=30))
    assert purge_finished(7) >= 1


def test_budget_grants_at_most_capacity_then_yields():
    """1 QPS、容量 5：一次最多放 5 个，随后立刻退让 —— 这就是「不平滑失效」的实现。"""
    budget.refill_now()
    capacity = int(budget.capacity())
    granted = budget.acquire_up_to(50)
    assert granted == capacity
    assert budget.acquire_up_to(50) == 0


def test_budget_never_goes_negative():
    budget.refill_now()
    budget.acquire_up_to(int(budget.capacity()) + 10)
    assert budget.stats()["tokens"] >= 0
