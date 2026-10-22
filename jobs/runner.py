"""worker 主循环（架构文档 §5.3）。

一个进程同时承担两件事：
* 调度线程：每 10 秒做一次 ``schedule_tick``（取到期 target → 扣预算 → 推进 due → 投递）；
* 执行线程池：从任务表 claim 任务并按 kind 分发。

没有 Redis、没有 Celery、没有 beat —— 任务就是一张表，进程重启自然续跑。
"""

from __future__ import annotations

import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections, connection
from django.utils import timezone

from core.config import get_settings
from core.log import get_logger, setup_logging
from jobs.handlers import get_handler
from jobs.queue import claim, complete, enqueue, fail, new_worker_id, reclaim_stale

__all__ = ["Worker"]

log = get_logger("worker")

SCHEDULE_TICK_SECONDS = 10
PERIODIC_INTERVAL_S = 60


class Worker:
    PERIODIC_INTERVAL_S = PERIODIC_INTERVAL_S

    def __init__(self, concurrency: int = 2, *, with_scheduler: bool = True) -> None:
        self.concurrency = max(1, concurrency)
        self.with_scheduler = with_scheduler
        self.worker_id = new_worker_id()
        self._stop = threading.Event()
        self._last_tick = 0.0
        self._last_reclaim = 0.0
        self._last_periodic = 0.0
        self._inflight = 0
        self._inflight_lock = threading.Lock()

    # ------------------------------------------------------------------ 生命周期
    def request_stop(self, *_args) -> None:
        log.info("worker.stopping", extra={"worker_id": self.worker_id})
        self._stop.set()

    def _install_signals(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.request_stop)
            except (ValueError, OSError):
                continue

    # ------------------------------------------------------------------ 调度
    def _maybe_tick(self) -> None:
        now = time.monotonic()
        if now - self._last_tick < SCHEDULE_TICK_SECONDS:
            return
        self._last_tick = now
        job = enqueue(
            "schedule_tick",
            {"limit": 200},
            idempotency_key=f"tick:{int(time.time() // SCHEDULE_TICK_SECONDS)}",
            priority=1,
        )
        if job is not None:
            log.debug("worker.tick_enqueued", extra={"job_id": job.id})

    def _maybe_periodic(self) -> None:
        """投递周期任务：小时指标 / 沉默告警每小时一次，日报按小时窗口每日一次。

        这三件事以前只写在 handler 注册表里、从没人投递 —— 结果是保留期清理、
        沉默告警、指标看板全都不存在。用幂等键保证「同一小时只投一次」，
        再靠 Job 表本身记住上次是否已跑，不需要额外引入 beat 组件。
        """
        now = time.monotonic()
        if now - self._last_periodic < self.PERIODIC_INTERVAL_S:
            return
        self._last_periodic = now

        local = timezone.localtime()
        hour_key = f"{local:%Y%m%d%H}"
        enqueue("health_snapshot", {}, idempotency_key=f"health:{hour_key}", priority=50)
        enqueue("silence_check", {}, idempotency_key=f"silence:{hour_key}", priority=50)

        digest_hour = get_settings().notify.digest_hour
        if local.hour == digest_hour:
            enqueue("daily_digest", {}, idempotency_key=f"digest:{local:%Y%m%d}", priority=50)

    def _maybe_reclaim(self) -> None:
        now = time.monotonic()
        if now - self._last_reclaim < 60:
            return
        self._last_reclaim = now
        recovered = reclaim_stale(older_than_seconds=300)
        if recovered:
            log.warning("worker.reclaimed_stale", extra={"count": recovered})

    # ------------------------------------------------------------------ 执行
    def _run_job(self, job) -> None:
        try:
            handler = get_handler(job.kind)
            if handler is None:
                raise RuntimeError(f"未知任务类型：{job.kind}")
            result = handler(job.payload or {})
            complete(job)
            log.info("job.done", extra={"job_id": job.id, "kind": job.kind, "result": _short(result)})
        except Exception as exc:  # 任何异常都要落库，不能静默
            state = fail(job, f"{type(exc).__name__}: {exc}")
            log.warning(
                "job.failed",
                extra={"job_id": job.id, "kind": job.kind, "next_state": state, "error": str(exc)[:300]},
            )
        finally:
            close_old_connections()

    def run_once(self, batch: int | None = None) -> int:
        """跑一轮（测试与一次性执行用）。返回处理的任务数。"""
        jobs = claim(batch or 10, self.worker_id)
        for job in jobs:
            self._run_job(job)
        return len(jobs)

    def run_forever(self) -> None:
        setup_logging("worker")
        self._install_signals()
        log.info("worker.started", extra={"worker_id": self.worker_id, "concurrency": self.concurrency})
        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="job") as pool:
            while not self._stop.is_set():
                close_old_connections()
                if self.with_scheduler:
                    self._maybe_tick()
                    self._maybe_periodic()
                self._maybe_reclaim()
                # 背压：线程池里已有任务在跑时不再 claim。
                # 否则任务会被提前置为 running 却没真正执行，租约到期后被回收 → 并发跑两遍。
                with self._inflight_lock:
                    free = self.concurrency - self._inflight
                if free <= 0:
                    time.sleep(0.2)
                    continue
                try:
                    jobs = claim(free, self.worker_id)
                except Exception as exc:
                    log.error("worker.claim_failed", extra={"error": str(exc)[:200]})
                    connection.close()
                    time.sleep(2)
                    continue
                for job in jobs:
                    with self._inflight_lock:
                        self._inflight += 1
                    pool.submit(self._run_job_safe, job)
                if not jobs:
                    time.sleep(0.5)
            pool.shutdown(wait=True, cancel_futures=False)
        log.info("worker.stopped", extra={"worker_id": self.worker_id})

    def _run_job_safe(self, job) -> None:
        try:
            self._run_job(job)
        finally:
            with self._inflight_lock:
                self._inflight = max(0, self._inflight - 1)
            connection.close()


def _short(result) -> dict:
    if not isinstance(result, dict):
        return {}
    return {k: v for k, v in list(result.items())[:6] if not isinstance(v, (list, dict))}


def main() -> None:
    import os

    concurrency = int(os.environ.get("WORKER_CONCURRENCY", "2"))
    with_scheduler = os.environ.get("WORKER_SCHEDULER", "1") not in {"0", "false", "no"}
    Worker(concurrency=concurrency, with_scheduler=with_scheduler).run_forever()


if __name__ == "__main__":
    main()
