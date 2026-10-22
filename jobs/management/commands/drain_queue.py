"""运行一次（跑空队列后退出），CI 与演示用。"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from jobs.runner import Worker


class Command(BaseCommand):
    help = "处理当前到期的任务直到队列为空，然后退出"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--rounds", type=int, default=20, help="最多跑几轮")
        parser.add_argument("--tick", action="store_true", help="先投递一次调度 tick")

    def handle(self, *args, **options) -> None:
        worker = Worker(concurrency=1, with_scheduler=False)
        if options["tick"]:
            from jobs.queue import enqueue

            enqueue("schedule_tick", {"limit": 200}, priority=1)
        total = 0
        for _ in range(options["rounds"]):
            processed = worker.run_once(batch=20)
            total += processed
            if processed == 0:
                break
        self.stdout.write(self.style.SUCCESS(f"processed {total} jobs"))
