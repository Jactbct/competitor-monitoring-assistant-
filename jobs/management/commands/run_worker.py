"""启动 worker 进程：``python manage.py run_worker --concurrency 2``。"""

from __future__ import annotations

import django
from django.core.management.base import BaseCommand

from jobs.runner import Worker


class Command(BaseCommand):
    help = "启动采集/分析/推送 worker（单进程，内含调度线程与执行线程池）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--concurrency", type=int, default=2, help="执行线程数")
        parser.add_argument("--no-scheduler", action="store_true", help="不承担调度职责（多 worker 时只留一个）")

    def handle(self, *args, **options) -> None:
        django.setup()
        Worker(concurrency=options["concurrency"], with_scheduler=not options["no_scheduler"]).run_forever()
