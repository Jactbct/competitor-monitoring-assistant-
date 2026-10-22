"""给浏览器 E2E 准备确定性数据：确保演示租户至少有一条「待处理建议」。

为什么需要独立命令：E2E 会真的点「采纳」，跑几次就会把待处理建议消耗光，
然后「查看 → 抽屉」这步会因为列表为空而失败 —— 那种失败看起来像 bug，其实是测试数据问题。
E2E 前置跑一次本命令，就永远有数据可点。
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from core.config import get_settings
from core.db import tenant_atomic
from core.tenant import tenant_scope
from shared.models import Snapshot
from tenancy.models import Suggestion, Tenant


class Command(BaseCommand):
    help = "为浏览器 E2E 造一条待处理建议（幂等；生产环境拒绝执行）"

    def handle(self, *args, **options) -> None:
        if get_settings().is_prod:
            raise CommandError("生产环境不允许生成 E2E 数据")

        tenant = Tenant.objects.filter(name="星野服饰").first()
        if tenant is None:
            raise CommandError("找不到演示租户，请先运行 python manage.py seed_demo")

        # 必须带租户上下文：app_rw 角色下 RLS 的 WITH CHECK 会拒绝无上下文的写
        with tenant_scope(str(tenant.id)), tenant_atomic(str(tenant.id)):
            existing = Suggestion.all_objects.filter(
                tenant=tenant, status=Suggestion.STATUS_PENDING
            ).first()
        if existing is not None:
            self.stdout.write(f"已有待处理建议：{existing.id}")
            return

        snapshot = (
            Snapshot.objects.filter(status=Snapshot.STATUS_VALID).order_by("-captured_at").first()
        )
        if snapshot is None:
            raise CommandError("没有可用快照，请先跑 drain_queue --tick")

        price = snapshot.price_final or snapshot.price_display or "—"
        card = {
            "conclusion": f"竞品「{snapshot.fields.get('title', '商品')[:20]}」到手价降至 {price} 元，建议人工核算后跟价",
            "evidence": [
                {
                    "snapshot_evidence_id": snapshot.evidence_id,
                    "quote": (snapshot.fields.get("evidence_blocks") or ["价格变化"])[0][:120],
                    "captured_at": snapshot.captured_at.isoformat(),
                }
            ],
            "action": "核对到手价后决定是否发券对齐，单件让利不超过 25 元",
            "owner_role": "operator",
            "deadline": (timezone.now() + timedelta(days=1)).isoformat(),
            "expected_impact": "价格对齐后预期恢复转化率",
            "cost_risk": {"mode": "not_applicable", "note": "E2E 样例数据"},
            "acceptance": "券上线后到手价对齐；7 天后回看价格是否回涨",
            "kind": "B_promo",
            "violations": [],
        }
        with tenant_scope(str(tenant.id)), tenant_atomic(str(tenant.id)):
            suggestion = Suggestion.all_objects.create(
                tenant=tenant,
                kind="B_promo",
                priority="P0",
                card=card,
                status=Suggestion.STATUS_PENDING,
                quality_gate="pass",
                target_id=snapshot.target_id,
            )
        self.stdout.write(self.style.SUCCESS(f"已创建待处理建议：{suggestion.id}（vendor={connection.vendor}）"))
