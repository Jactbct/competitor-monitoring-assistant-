"""判据报告：把「能不能开 B 类（跟价）自动推送」变成一条命令。

为什么需要它：P-1 的闸门是「到手价可确定率 ≥70%」，但这个数字如果不落到一条能跑的命令上，
就会变成「谁记得谁估一个」。每次发版前跑一次，数字写进发布记录。

判据与门槛（来自《能力规格》§6）：

| 指标 | 门槛 | 不达标意味着 |
|---|---|---|
| 到手价可确定率 | ≥ 70% | 不能开 B 类：调价建议会建立在错的到手价上 |
| 降级率 | ≤ 15% | prompt / 数据源 / 页面结构有问题 |
| 8 字段齐全率 | 100% | 卡片不合格，应进人工确认队列 |
| 证据回链率 | 100% | 建议引用的快照已找不到，等于不可复核 |
| 采纳率 | ≥ 30%（观察项） | 建议不可执行，或推送对象不对 |
| P95 事件→建议时延 | ≤ 90s | 队列/worker 有问题 |

用法：
    python manage.py gate_report                 # 最近 7 天
    python manage.py gate_report --days 30
    python manage.py gate_report --gate           # 门槛不达标时退出码 1（CI 用）
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from shared.models import ChangeEvent, Snapshot
from tenancy.models import Suggestion

CARD_FIELDS = (
    "conclusion",
    "evidence",
    "action",
    "owner_role",
    "deadline",
    "expected_impact",
    "cost_risk",
    "acceptance",
)


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


class Command(BaseCommand):
    help = "输出发布前判据（到手价可确定率 / 降级率 / 字段齐全率 / 证据回链率 / 采纳率 / P95 时延）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--days", type=int, default=7, help="统计窗口（天）")
        parser.add_argument("--gate", action="store_true", help="门槛不达标时以退出码 1 结束")

    def handle(self, *args, **options) -> None:
        days = options["days"]
        since = timezone.now() - timedelta(days=days)

        valid = Snapshot.objects.filter(captured_at__gte=since, status=Snapshot.STATUS_VALID)
        valid_total = valid.count()
        certain = valid.filter(price_confidence="certain").count()
        ratio = (certain / valid_total) if valid_total else 0.0

        abnormal = Snapshot.objects.filter(captured_at__gte=since, status=Snapshot.STATUS_ABNORMAL).count()
        total_snap = valid_total + abnormal
        abnormal_rate = (abnormal / total_snap) if total_snap else 0.0

        suggestions = list(Suggestion.all_objects.filter(created_at__gte=since))
        total_sug = len(suggestions)
        degraded = sum(1 for s in suggestions if s.degraded)
        degraded_rate = (degraded / total_sug) if total_sug else 0.0

        complete = sum(
            1 for s in suggestions if all((s.card or {}).get(f) for f in CARD_FIELDS)
        )
        complete_rate = (complete / total_sug) if total_sug else 0.0

        evidence_ids = {
            str(e.get("snapshot_evidence_id"))
            for s in suggestions
            for e in ((s.card or {}).get("evidence") or [])
            if e.get("snapshot_evidence_id")
        }
        resolvable = (
            Snapshot.objects.filter(evidence_id__in=evidence_ids).count() if evidence_ids else 0
        )
        evidence_rate = (resolvable / len(evidence_ids)) if evidence_ids else 1.0

        accepted = sum(1 for s in suggestions if s.status == Suggestion.STATUS_ACCEPTED)
        decided = sum(
            1
            for s in suggestions
            if s.status in {Suggestion.STATUS_ACCEPTED, Suggestion.STATUS_IGNORED, Suggestion.STATUS_FALSE_POSITIVE}
        )
        accept_rate = (accepted / decided) if decided else None

        # 事件 → 建议 的时延：靠 event_id 关联，取 P95
        event_ids = [s.event_id for s in suggestions if s.event_id]
        detected = dict(
            ChangeEvent.objects.filter(id__in=event_ids).values_list("id", "detected_at")
        )
        latencies = [
            (s.created_at - detected[s.event_id]).total_seconds()
            for s in suggestions
            if s.event_id in detected
        ]
        p95 = _p95(latencies)

        rows = [
            ("到手价可确定率", f"{ratio:.1%}", "≥ 70%", ratio >= 0.70),
            ("采集异常率", f"{abnormal_rate:.1%}", "≤ 2%", abnormal_rate <= 0.02),
            ("分析降级率", f"{degraded_rate:.1%}", "≤ 15%", degraded_rate <= 0.15),
            ("8 字段齐全率", f"{complete_rate:.1%}", "= 100%", complete_rate >= 0.999),
            ("证据回链率", f"{evidence_rate:.1%}", "= 100%", evidence_rate >= 0.999),
            (
                "采纳率",
                "无决策数据" if accept_rate is None else f"{accept_rate:.1%}",
                "≥ 30%（观察项）",
                None if accept_rate is None else accept_rate >= 0.30,
            ),
            (
                "P95 事件→建议",
                "无数据" if p95 is None else f"{p95:.0f}s",
                "≤ 90s",
                None if p95 is None else p95 <= 90,
            ),
        ]

        self.stdout.write(f"\n统计窗口：最近 {days} 天（{since:%Y-%m-%d %H:%M} 起）")
        self.stdout.write(
            f"样本：有效快照 {valid_total} · 异常快照 {abnormal} · 变化事件 "
            f"{ChangeEvent.objects.filter(detected_at__gte=since).count()} · 建议 {total_sug}\n"
        )
        self.stdout.write(f"{'指标':<16}{'实测':>12}   {'门槛':<16}{'结果'}")
        self.stdout.write("-" * 58)
        for name, value, threshold, ok in rows:
            mark = "—" if ok is None else ("PASS" if ok else "FAIL")
            self.stdout.write(f"{name:<16}{value:>12}   {threshold:<16}{mark}")

        hard_fail = [name for name, _v, _t, ok in rows if ok is False]
        b_gate = ratio >= 0.70

        self.stdout.write("\n结论：")
        if valid_total == 0:
            self.stdout.write("  样本不足（没有有效快照），先让采集跑起来再判断")
        elif b_gate:
            self.stdout.write("  B 类（跟价）闸门 **可打开**：到手价可确定率已达 70%")
            self.stdout.write("  打开方式：B_CLASS_ENABLED=1，并观察一周降级率与采纳率")
        else:
            import math

            need = math.ceil(0.70 * valid_total) - certain
            self.stdout.write(
                f"  B 类闸门 **未达**：还需 {max(need, 0)} 条可确定到手价（当前 {ratio:.1%}）"
            )
            self.stdout.write("  期间价格类变化仍按「价格提示卡 + 人工确认」处理（quality_gate=manual_required）")

        if hard_fail:
            self.stdout.write(self.style.WARNING(f"  未达标项：{', '.join(hard_fail)}"))
        else:
            self.stdout.write(self.style.SUCCESS("  全部硬门槛通过"))

        if options["gate"] and hard_fail:
            raise SystemExit(1)
