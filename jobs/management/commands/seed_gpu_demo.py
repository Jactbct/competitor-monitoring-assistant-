"""显卡类目演示：一个竞品 + 我方一个在售 SKU。

背景：用户要求「拿 RTX 5060 监控跑几天」。本机出口访问京东会被 302 回首页（反爬），
所以这里用 `tests/fixtures/jd/` 下三个合成页面代表「三天」的价格与促销演进，
跑完全真实的流水线（抽取 → 到手价 → diff → LLM 归因 → 建议卡 → 效果回看）。

幂等：重复执行不会重复建租户/商品/监控对象。
生产环境拒绝执行。
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.config import get_settings
from domain.collect.guardrails import normalize_url, platform_for, url_hash
from shared.models import Target
from tenancy.models import AppUser, SelfProduct, Tenant, TenantTarget

TENANT_NAME = "极光数码"
USER_EMAIL = "gpu@demo.local"
PASSWORD = "demo12345"

TARGET_URL = "https://item.jd.com/10012345678.html"
TARGET_TITLE = "影驰 GeForce RTX 5060 金属大师 8G GDDR7 电竞游戏显卡"

OUR_SKU = "GPU-5060-OURS"
OUR_TITLE = "RTX 5060 金属大师 8G（我方在售）"
OUR_PRICE = Decimal("2599.00")
OUR_COST = Decimal("1999.00")  # 毛利 (2599-1999)/2599 ≈ 23.1%
OUR_FLOOR = Decimal("0.22")


class Command(BaseCommand):
    help = "创建显卡类目演示租户：1 个竞品监控对象 + 1 个我方在售商品（幂等）"

    def handle(self, *args, **options) -> None:
        if get_settings().is_prod:
            raise CommandError("生产环境不允许生成演示数据")

        tenant, created = Tenant.objects.get_or_create(
            name=TENANT_NAME, defaults={"plan": "pro", "quota_json": {"targets": 50}}
        )
        self.stdout.write(f"租户：{tenant.name} {'(新建)' if created else '(已存在)'}")

        user, user_created = AppUser.objects.get_or_create(
            email=USER_EMAIL,
            defaults={"tenant": tenant, "role": AppUser.ROLE_ADMIN},
        )
        if user_created:
            user.set_password(PASSWORD)
            user.save(update_fields=["password"])
        self.stdout.write(f"账号：{USER_EMAIL} / {PASSWORD} {'(新建)' if user_created else '(已存在)'}")

        product, product_created = SelfProduct.all_objects.get_or_create(
            tenant=tenant,
            sku=OUR_SKU,
            defaults={
                "title": OUR_TITLE,
                "price_display": OUR_PRICE,
                "cost": OUR_COST,
                "margin_floor": OUR_FLOOR,
                "attrs": {"品牌": "影驰(GALAXY)", "显存": "8GB GDDR7", "型号": "RTX 5060 金属大师"},
            },
        )
        self.stdout.write(
            f"我方商品：{product.sku} 售价 {product.price_display} 成本 {product.cost} "
            f"{'(新建)' if product_created else '(已存在)'}"
        )

        normalized = normalize_url(TARGET_URL)
        target, target_created = Target.objects.get_or_create(
            platform=platform_for(normalized),
            url_hash=url_hash(normalized),
            defaults={
                "url": normalized,
                "status": Target.STATUS_ACTIVE,
                "item_id": "10012345678",
                "last_title": TARGET_TITLE,
                "interval_min": 120,  # 2 小时一次：合规且够用
                "next_due_at": timezone.now(),
            },
        )
        self.stdout.write(
            f"监控对象：{target.id} {TARGET_TITLE[:24]}… 每 {target.interval_min} 分钟 "
            f"{'(新建)' if target_created else '(已存在)'}"
        )

        TenantTarget.all_objects.get_or_create(
            tenant=tenant,
            target_id=target.id,
            self_product=product,
            defaults={"priority": "core", "enabled": True},
        )
        self.stdout.write(self.style.SUCCESS("绑定完成：core 优先级（P0 实时、4 小时内处理要求）"))
