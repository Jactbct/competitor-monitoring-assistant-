"""生成一套可演示、可回归的演示数据（幂等，可重复执行）。

做三件事：
1. 两个租户（A 有数据、B 空租户），用于验证隔离；
2. 三个竞品目标 + 三个自有 SKU，并**预置一份「变化前」快照**；
3. 把 ``next_due_at`` 设为当前时间，worker 一跑就能产生真实变化事件与建议。

预置「变化前」快照是关键：否则第一次采集没有对比基线，演示里看不到任何变化。
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.config import get_settings
from domain.extract.rules import extract
from shared.models import Snapshot, Target, TargetDemand
from tenancy.models import AppUser, SelfProduct, Tenant, TenantTarget

DEMO_PASSWORD = "demo12345"

TARGETS = (
    {
        "item_id": "1001",
        "priority": TenantTarget.PRIORITY_CORE,
        "sku": "TS-COTTON-001",
        "title_our": "纯棉短袖T恤男 夏季 宽松 五分袖 打底衫",
    },
    {
        "item_id": "1002",
        "priority": TenantTarget.PRIORITY_NORMAL,
        "sku": "TS-ICE-002",
        "title_our": "冰丝速干T恤男 运动健身 短袖",
    },
    {
        "item_id": "1003",
        "priority": TenantTarget.PRIORITY_LONG_TAIL,
        "sku": "CD-COTTON-003",
        "title_our": "针织开衫女 秋季 外搭 毛衣外套",
    },
)

PRODUCTS = {
    "TS-COTTON-001": {"title": "纯棉短袖T恤男 夏季 宽松", "price": "139.00", "cost": "78.00"},
    "TS-ICE-002": {"title": "冰丝速干T恤男 运动", "price": "95.00", "cost": "52.00"},
    "CD-COTTON-003": {"title": "针织开衫女 秋季 外搭", "price": "299.00", "cost": "196.00"},
}


def _url(item_id: str) -> str:
    return f"https://item.taobao.com/item.htm?id={item_id}"


class Command(BaseCommand):
    help = "生成演示数据（租户/用户/商品/竞品/变化前快照）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--reset", action="store_true", help="先清空演示租户的业务数据")

    def handle(self, *args, **options) -> None:
        settings = get_settings()
        if settings.is_prod:
            raise CommandError(
                "拒绝在 APP_ENV=prod 下生成演示数据（含固定口令账号）；"
                "生产建账号请用 createsuperuser 或设置页添加成员"
            )
        before_dir = settings.collector.fixture_dir.parent / "pages_before"

        tenant, _ = Tenant.objects.get_or_create(
            name="星野服饰",
            defaults={"plan": "pro", "quota_json": {"targets": 100}},
        )
        if options["reset"]:
            TenantTarget.all_objects.filter(tenant=tenant).delete()
            Snapshot.objects.all().delete()
            Target.objects.all().delete()

        for email, role in (
            ("ops@demo.local", AppUser.ROLE_ADMIN),
            ("buyer@demo.local", AppUser.ROLE_OPERATOR),
            ("boss@demo.local", AppUser.ROLE_VIEWER),
        ):
            user, created = AppUser.objects.get_or_create(
                email=email,
                defaults={"tenant": tenant, "role": role, "status": "active"},
            )
            if created:
                user.set_password(DEMO_PASSWORD)
                user.save()

        platform_user, created = AppUser.objects.get_or_create(
            email="platform@demo.local",
            defaults={"tenant": None, "role": AppUser.ROLE_PLATFORM_OPS, "status": "active"},
        )
        if created:
            platform_user.set_password(DEMO_PASSWORD)
            platform_user.save()
            platform_user.is_superuser = True
            platform_user.save(update_fields=["is_superuser"])

        # 第二个租户：用于验证「看不到别人的数据」
        Tenant.objects.get_or_create(name="对照租户 B", defaults={"plan": "basic"})

        products: dict[str, SelfProduct] = {}
        for sku, spec in PRODUCTS.items():
            product, created = SelfProduct.all_objects.get_or_create(
                tenant=tenant,
                sku=sku,
                defaults={
                    "title": spec["title"],
                    "price_display": spec["price"],
                    "cost": spec["cost"],
                    "margin_floor": "0.22",
                },
            )
            products[sku] = product

        now = timezone.now()
        for spec in TARGETS:
            url = _url(spec["item_id"])
            from domain.collect.guardrails import normalize_url, url_hash

            normalized = normalize_url(url)
            target, _ = Target.objects.get_or_create(
                platform="taobao",
                url_hash=url_hash(normalized),
                defaults={
                    "url": normalized,
                    "source": "public_sample",
                    "next_due_at": now,
                    "interval_min": TenantTarget.PRIORITY_BY_INTERVAL[spec["priority"]],
                },
            )
            target.next_due_at = now
            target.interval_min = TenantTarget.PRIORITY_BY_INTERVAL[spec["priority"]]
            target.save(update_fields=["next_due_at", "interval_min"])

            TenantTarget.all_objects.get_or_create(
                tenant=tenant,
                target_id=target.id,
                self_product=products[spec["sku"]],
                defaults={"priority": spec["priority"], "enabled": True},
            )
            TargetDemand.objects.update_or_create(
                target_id=target.id,
                defaults={
                    "has_monitor": True,
                    "min_interval_min": TenantTarget.PRIORITY_BY_INTERVAL[spec["priority"]],
                },
            )

            # 预置「变化前」快照（3 小时前），让第一次采集能算出变化
            if not Snapshot.objects.filter(target=target, status=Snapshot.STATUS_VALID).exists():
                fixture = before_dir / f"{spec['item_id']}.html"
                if not fixture.is_file():
                    self.stderr.write(f"缺少基线样本页：{fixture}")
                    continue
                html = fixture.read_text(encoding="utf-8")
                ex = extract(html, normalized)
                fields = ex.to_snapshot_fields()
                fields["price_final"] = str(ex.price.final) if ex.price and ex.price.final else None
                from domain.diff.diff import fingerprint

                fields["fingerprint"] = fingerprint(fields)
                Snapshot.objects.create(
                    target=target,
                    captured_at=now - timedelta(hours=3),
                    http_status=200,
                    content_sha256=fingerprint(fields),
                    extract_version=ex.extract_version,
                    fields=fields,
                    field_completeness=ex.completeness,
                    price_display=ex.price.display if ex.price else None,
                    price_final=ex.price.final if ex.price else None,
                    price_confidence=ex.price.confidence if ex.price else "uncertain",
                    promo_detail=[i.to_dict() for i in ex.promo_items],
                    evidence_id=f"snap-base{spec['item_id']}",
                    status=Snapshot.STATUS_VALID,
                )
                target.last_title = str(fields.get("title") or "")[:200]
                target.last_price_display = ex.price.display if ex.price else None
                target.last_price_final = ex.price.final if ex.price else None
                target.last_price_confidence = ex.price.confidence if ex.price else "uncertain"
                target.last_captured_at = now - timedelta(hours=3)
                target.last_status = "valid"
                target.save()

        self.stdout.write(
            self.style.SUCCESS(
                f"演示数据就绪：租户 {tenant.name}，用户 ops@demo.local / {DEMO_PASSWORD}，"
                f"平台运营 platform@demo.local / {DEMO_PASSWORD}"
            )
        )
