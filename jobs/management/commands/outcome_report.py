"""效果回看报表：建议到底有没有用。

「采纳率」只能说明运营点了按钮，说明不了建议是否有效。这张报表回答的是：
采纳 7 天后，竞品价格回涨了（说明原降价是短期活动）、维持了、还是继续下探。

用法：
    python manage.py outcome_report                 # 全部已测
    python manage.py outcome_report --days 30       # 最近 30 天采纳的
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Avg, Count
from django.utils import timezone

from tenancy.models import Outcome

VERDICT_LABEL = {
    Outcome.PRICE_REVERTED: "价格回涨（原降价是短期活动）",
    Outcome.PRICE_KEPT: "价格维持（建议仍有效）",
    Outcome.PRICE_FURTHER_DOWN: "继续下探（需重新评估）",
    Outcome.UNAVAILABLE: "取不到价（不计入效果判断）",
    Outcome.PENDING: "未到期",
}


class Command(BaseCommand):
    help = "输出建议采纳后的 T+7 效果回看分布"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--days", type=int, default=0, help="只看最近 N 天采纳的（0=全部）")

    def handle(self, *args, **options) -> None:
        query = Outcome.all_objects.all()
        if options["days"]:
            query = query.filter(accepted_at__gte=timezone.now() - timedelta(days=options["days"]))

        total = query.count()
        days = options["days"]
        window = "全部" if not days else f"最近 {days} 天"
        self.stdout.write(f"\n采纳记录：{total} 条（窗口：{window}）")

        if total == 0:
            self.stdout.write("  还没有采纳记录：先在建议中心采纳一条，7 天后会自动回看")
            return

        rows = list(query.values("outcome").annotate(n=Count("id")).order_by("-n"))
        measured = sum(row["n"] for row in rows if row["outcome"] != Outcome.PENDING)
        avg_change = query.filter(measured_at__isnull=False).aggregate(v=Avg("change_pct"))["v"]

        self.stdout.write("\n分布：")
        for row in rows:
            label = VERDICT_LABEL.get(row["outcome"], row["outcome"])
            self.stdout.write(f"  {label:<28} {row['n']:>4} 条")

        if measured:
            reverted = query.filter(outcome=Outcome.PRICE_REVERTED).count()
            kept = query.filter(outcome=Outcome.PRICE_KEPT).count()
            further = query.filter(outcome=Outcome.PRICE_FURTHER_DOWN).count()
            self.stdout.write(f"\n已测 {measured} 条：回涨 {reverted} · 维持 {kept} · 下探 {further}")
            if avg_change is not None:
                self.stdout.write(f"平均价格变化：{avg_change:.2%}（负=继续下跌）")
            if reverted / measured >= 0.5:
                self.stdout.write(
                    self.style.WARNING(
                        "  回涨占比过半：说明多数降价是短期活动，跟价建议应更强调「限时券」而不是改挂牌价"
                    )
                )
        else:
            self.stdout.write("\n  暂无到期回看（T+7 还没到）")

        pending = query.filter(outcome=Outcome.PENDING).count()
        if pending:
            self.stdout.write(f"  待回看：{pending} 条")
