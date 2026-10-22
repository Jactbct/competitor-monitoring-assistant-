"""端到端闭环：采集 → 快照 → 事件 → 分析 → 建议（fixture 模式，不产生外部请求）。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from jobs.handlers.analyze import handle_analyze
from jobs.handlers.collect import handle_collect
from shared.models import ChangeEvent, Snapshot
from tenancy.models import Suggestion

pytestmark = pytest.mark.django_db


def test_collect_detects_change_and_creates_event(
    target, baseline_snapshot, binding, product
):
    result = handle_collect({"target_id": target.id})

    assert result["changed"] is True
    event = ChangeEvent.objects.get(pk=result["event_id"])
    assert set(event.event_types) >= {"real_price", "coupon_promo"}
    assert event.magnitude is not None and event.magnitude > 0

    latest = Snapshot.objects.filter(target=target, status=Snapshot.STATUS_VALID).order_by("-captured_at").first()
    assert latest.price_final == 114  # 129 - 15 的店铺券
    assert latest.price_confidence == "certain"
    assert latest.raw_object_key  # 原始页已归档（7 天后清理）


def test_second_collect_same_content_produces_no_new_event(
    target, baseline_snapshot, binding, product
):
    first = handle_collect({"target_id": target.id})
    assert first["changed"] is True
    events_before = ChangeEvent.objects.count()

    second = handle_collect({"target_id": target.id})

    assert second["changed"] is False
    assert ChangeEvent.objects.count() == events_before


def test_analyze_produces_suggestion_with_eight_fields(
    target, baseline_snapshot, binding, product, tenant
):
    collected = handle_collect({"target_id": target.id})
    result = handle_analyze(
        {"event_id": collected["event_id"], "tenant_id": str(tenant.id), "self_product_id": str(product.id)}
    )

    suggestion = Suggestion.all_objects.get(pk=result["suggestion_id"])
    assert suggestion.priority == "P0"
    assert suggestion.quality_gate == "manual_required"  # B 类闸门默认关闭
    card = suggestion.card
    for field in (
        "conclusion",
        "evidence",
        "action",
        "owner_role",
        "deadline",
        "expected_impact",
        "cost_risk",
        "acceptance",
    ):
        assert card.get(field), f"卡片缺字段：{field}"
    assert card["cost_risk"]["breakeven_volume_ratio"] == "1.39"


def test_analyze_is_idempotent(target, baseline_snapshot, binding, product, tenant):
    collected = handle_collect({"target_id": target.id})
    payload = {
        "event_id": collected["event_id"],
        "tenant_id": str(tenant.id),
        "self_product_id": str(product.id),
    }
    first = handle_analyze(payload)
    second = handle_analyze(payload)
    assert first["suggestion_id"] == second["suggestion_id"]
    assert Suggestion.all_objects.filter(tenant=tenant).count() == 1


def test_analyze_skips_when_binding_removed(target, baseline_snapshot, binding, product, tenant):
    collected = handle_collect({"target_id": target.id})
    binding.enabled = False
    binding.save(update_fields=["enabled"])

    result = handle_analyze(
        {"event_id": collected["event_id"], "tenant_id": str(tenant.id), "self_product_id": str(product.id)}
    )

    assert result["skipped"] == "no_binding"


def test_abnormal_page_does_not_create_event(target, binding, product, monkeypatch):
    """结构改版页必须判异常：不产生快照有效记录、不产生事件、并把目标标异常。"""
    from domain.collect.client import FetchResult

    html = "<html><body><div class='nothing'></div></body></html>"
    monkeypatch.setattr(
        "jobs.handlers.collect.fetch_page",
        lambda url, **kwargs: FetchResult(status_code=200, body=html.encode() * 40, final_url=url, elapsed_ms=10),
    )

    result = handle_collect({"target_id": target.id})

    assert result["abnormal"] == "STRUCTURE_CHANGED"
    assert ChangeEvent.objects.count() == 0
    target.refresh_from_db()
    assert target.last_status == "abnormal"
    assert target.fail_streak == 1


def test_circuit_opens_after_threshold(target, binding, product, monkeypatch):
    from domain.collect.client import FetchResult

    html = "<html><body>访问受限</body></html>"
    monkeypatch.setattr(
        "jobs.handlers.collect.fetch_page",
        lambda url, **kwargs: FetchResult(status_code=429, body=html.encode() * 200, final_url=url, elapsed_ms=5),
    )

    for _ in range(5):
        handle_collect({"target_id": target.id})

    target.refresh_from_db()
    assert target.circuit_state == "open"
    assert target.circuit_until is not None


def test_not_modified_skips_snapshot(target, baseline_snapshot, binding, product, monkeypatch):
    """304 表示页面没变：不落快照、不判异常，只推进 due（最省预算的一轮）。"""
    from domain.collect.client import FetchResult

    monkeypatch.setattr(
        "jobs.handlers.collect.fetch_page",
        lambda url, **kwargs: FetchResult(
            status_code=304, body=b"", final_url=url, elapsed_ms=8, source="http", not_modified=True
        ),
    )

    before = Snapshot.objects.count()
    result = handle_collect({"target_id": target.id})

    assert result["not_modified"] is True
    assert Snapshot.objects.count() == before
    target.refresh_from_db()
    assert target.last_304_at is not None
    assert target.last_status != "abnormal"


def test_retry_after_extends_circuit_cooldown(target, binding, product, monkeypatch):
    """站点给了 Retry-After，就按它说的等，而不是拍一个固定冷却时间。"""
    from domain.collect.client import FetchResult

    monkeypatch.setattr(
        "jobs.handlers.collect.fetch_page",
        lambda url, **kwargs: FetchResult(
            status_code=429,
            body=b"<html>" + b"x" * 4000 + b"</html>",
            final_url=url,
            elapsed_ms=5,
            source="http",
            retry_after_s=7200,
        ),
    )

    for _ in range(5):
        handle_collect({"target_id": target.id})

    target.refresh_from_db()
    assert target.circuit_state == "open"
    assert target.circuit_until is not None
    # 7200s ≈ 121 分钟，明显大于默认 30 分钟冷却
    assert (target.circuit_until - timezone.now()) > timedelta(minutes=100)


@pytest.mark.django_db
def test_schedule_tick_respects_budget(target, binding):
    from infra import budget
    from jobs.handlers.collect import handle_schedule_tick

    budget.refill_now()
    result = handle_schedule_tick({"limit": 200})
    assert result["enqueued"] >= 1

    # 把桶抽干并把 next_due_at 拉回当前 → 应该整轮退让，而不是继续投递
    from django.utils import timezone

    from shared.models import Target

    budget.acquire_up_to(int(budget.capacity()))
    Target.objects.filter(pk=target.pk).update(next_due_at=timezone.now())
    assert handle_schedule_tick({"limit": 200}).get("budget_exhausted") is True
