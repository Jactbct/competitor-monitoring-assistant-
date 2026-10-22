"""采集 handler：一条 target 的完整采集链路。

顺序不可交换（架构文档 §5.3）：
护栏/预算已在调度侧完成 → 抓取 → 异常识别 → 规则抽取 → 快照落库 → diff → 事件 → 扇出分析。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from uuid import uuid4

from django.db.models import F, Q
from django.utils import timezone

from core.config import get_settings
from core.log import get_logger
from domain.collect.abnormal import sniff_abnormal
from domain.collect.client import fetch_page
from domain.collect.guardrails import GuardrailError
from domain.diff.diff import diff as diff_fields
from domain.diff.diff import fingerprint
from domain.extract.rules import extract
from infra import budget, storage
from jobs.queue import enqueue
from shared.models import ChangeEvent, Snapshot, Target
from tenancy.services import fanout_tenants

__all__ = ["handle_collect", "handle_schedule_tick"]

log = get_logger("collect")


def _interval(target: Target) -> timedelta:
    """按站点活跃时段加权：凌晨降频到 1/3（补充文档 §7.6）。"""
    hour = timezone.localtime().hour
    low, high = get_settings().collector.active_hours
    factor = 1 if low <= hour < high else get_settings().collector.idle_interval_factor
    return timedelta(minutes=max(target.interval_min, 1) * factor)


def _record_failure(target: Target, reason: str, now, retry_after_s: int | None = None) -> None:
    threshold = get_settings().collector.circuit_fail_threshold
    # 原子自增：并发（回收后重跑）时 load-modify-save 会丢更新，把熔断状态覆盖回去
    Target.objects.filter(pk=target.pk).update(fail_streak=F("fail_streak") + 1)
    target.refresh_from_db(fields=["fail_streak"])
    target.last_status = "abnormal"
    target.next_due_at = now + _interval(target)
    if target.fail_streak >= threshold:
        cooldown = get_settings().collector.circuit_cooldown_min
        # 冷却时间按失败轮次翻倍，上限 24h
        multiplier = 2 ** min(target.fail_streak - threshold, 5)
        cooldown = min(cooldown * multiplier, get_settings().collector.circuit_max_cooldown_min)
        if retry_after_s:
            # 站点明确说了「多久之后再来」，就听它的（比我们自己拍的冷却时间更准也更礼貌）
            cooldown = max(cooldown, retry_after_s // 60 + 1)
            cooldown = min(cooldown, get_settings().collector.circuit_max_cooldown_min)
        target.circuit_state = Target.CIRCUIT_OPEN
        target.circuit_until = now + timedelta(minutes=cooldown)
    target.save(
        update_fields=["fail_streak", "last_status", "next_due_at", "circuit_state", "circuit_until"]
    )
    log.warning("collect.failed", extra={"target_id": target.id, "reason": reason[:200]})


def _record_success(target: Target, ex, now, crawl_delay_s: int | None = None) -> None:
    target.fail_streak = 0
    target.circuit_state = Target.CIRCUIT_CLOSED
    target.circuit_until = None
    target.last_ok_at = now
    target.last_captured_at = now
    target.last_status = Snapshot.STATUS_VALID
    target.last_title = str(ex.fields.get("title") or "")[:200]
    # 抓不到价时**显式清空**，不留上一次的值：否则「价格没变」和「没抓到价」在页面上长得一样
    target.last_price_display = ex.price.display if ex.price else None
    target.last_price_final = ex.price.final if ex.price else None
    target.last_price_confidence = ex.price.confidence if ex.price else "uncertain"
    interval = _interval(target)
    if crawl_delay_s:
        # robots 的 Crawl-delay 是站点明示的下限，取更保守的那个
        interval = max(interval, timedelta(seconds=crawl_delay_s))
    target.next_due_at = now + interval
    target.save(
        update_fields=[
            "fail_streak",
            "circuit_state",
            "circuit_until",
            "last_ok_at",
            "last_captured_at",
            "last_status",
            "last_title",
            "last_price_display",
            "last_price_final",
            "last_price_confidence",
            "next_due_at",
        ]
    )


def handle_collect(payload: dict) -> dict:
    target_id = int(payload["target_id"])
    target = Target.objects.filter(pk=target_id).first()
    if target is None:
        return {"skipped": "target_missing"}

    now = timezone.now()
    if target.circuit_state == Target.CIRCUIT_OPEN:
        if target.circuit_until and target.circuit_until > now:
            return {"skipped": "circuit_open"}
        # 冷却到期 → 半开，只放一次探针
        target.circuit_state = Target.CIRCUIT_HALF_OPEN
        target.save(update_fields=["circuit_state"])

    try:
        result = fetch_page(
            target.url, etag=target.http_etag, last_modified=target.http_last_modified
        )
    except GuardrailError as exc:
        _record_failure(target, f"guardrail:{exc}", now)
        return {"abnormal": "GUARDRAIL", "reason": str(exc)[:200]}
    except Exception as exc:  # 网络层异常统一按失败处理
        _record_failure(target, f"{type(exc).__name__}:{exc}", now)
        return {"abnormal": "NETWORK", "reason": type(exc).__name__}

    if result.not_modified:
        # 304：页面没变。不落快照、不算异常，只推进 due —— 这是最省预算的一轮采集
        target.last_304_at = now
        target.last_ok_at = now
        target.fail_streak = 0
        target.circuit_state = Target.CIRCUIT_CLOSED
        target.circuit_until = None
        target.next_due_at = now + _interval(target)
        target.save(
            update_fields=[
                "last_304_at",
                "last_ok_at",
                "fail_streak",
                "circuit_state",
                "circuit_until",
                "next_due_at",
            ]
        )
        log.info("collect.not_modified", extra={"target_id": target.id, "status": result.status_code})
        return {"not_modified": True}

    html = result.text
    ex = extract(html, target.url)
    abnormal = sniff_abnormal(
        http_status=result.status_code,
        body_bytes=len(result.body),
        text=html,
        strong_hits=ex.strong_hits,
    )

    if abnormal.is_abnormal:
        # 异常页也留一条快照用于排障，但绝不参与 diff、绝不生成事件
        Snapshot.objects.create(
            target=target,
            captured_at=now,
            http_status=result.status_code,
            content_sha256=hashlib.sha256(result.body).hexdigest(),
            extract_version=ex.extract_version,
            fields={"strong_markers": ex.strong_markers},
            field_completeness=ex.completeness,
            price_confidence="uncertain",
            evidence_id=f"snap-{uuid4().hex[:8]}",
            status=Snapshot.STATUS_ABNORMAL,
            abnormal_type=abnormal.abnormal_type,
        )
        _record_failure(
            target,
            f"{abnormal.abnormal_type}:{abnormal.reason}",
            now,
            retry_after_s=result.retry_after_s,
        )
        return {"abnormal": abnormal.abnormal_type, "reason": abnormal.reason}

    body_sha = hashlib.sha256(result.body).hexdigest()
    stored = storage.save_raw(target.id, body_sha, result.body, now)
    price = ex.price
    fields = ex.to_snapshot_fields()
    fields["price_final"] = str(price.final) if price and price.final is not None else None
    fields["fingerprint"] = fingerprint(fields)

    snapshot = Snapshot.objects.create(
        target=target,
        captured_at=now,
        http_status=result.status_code,
        content_sha256=body_sha,
        raw_object_key=stored.key,
        raw_bytes=len(result.body),
        extract_version=ex.extract_version,
        fields=fields,
        field_completeness=ex.completeness,
        price_display=price.display if price else None,
        price_final=price.final if price else None,
        price_confidence=price.confidence if price else "uncertain",
        promo_detail=[item.to_dict() for item in ex.promo_items],
        evidence_id=f"snap-{uuid4().hex[:8]}",
        status=Snapshot.STATUS_VALID,
    )

    previous = (
        Snapshot.objects.filter(target=target, status=Snapshot.STATUS_VALID, id__lt=snapshot.id)
        .order_by("-captured_at")
        .first()
    )


    changes = diff_fields(previous.fields if previous else {}, fields)
    if result.etag:
        target.http_etag = result.etag
    if result.last_modified:
        target.http_last_modified = result.last_modified
    _record_success(target, ex, now, crawl_delay_s=result.crawl_delay_s)
    if result.etag or result.last_modified:
        Target.objects.filter(pk=target.pk).update(
            http_etag=target.http_etag, http_last_modified=target.http_last_modified
        )

    if previous is None or changes.is_empty:
        return {"snapshot": snapshot.evidence_id, "changed": False}

    window_start = now - timedelta(minutes=30)
    recent = (
        ChangeEvent.objects.filter(target=target, detected_at__gte=window_start)
        .order_by("-detected_at")
        .first()
    )
    if recent is not None and set(recent.event_types or []) == set(changes.event_types or []):
        # 同一目标 30 分钟内同类变化 → 合并进既有事件（价格往复不刷屏），
        # 但把快照换成最新的，保证建议卡用的是当前页面而不是窗口起始那份
        ChangeEvent.objects.filter(pk=recent.pk).update(
            snapshot=snapshot,
            prev_snapshot_id=previous.id,
            changed_fields=changes.changed_fields,
            deltas=changes.deltas,
            magnitude=changes.magnitude,
            merged_count=F("merged_count") + 1,
        )
        event = ChangeEvent.objects.get(pk=recent.pk)
        created = False
    else:
        event, created = ChangeEvent.objects.get_or_create(
            target=target,
            snapshot=snapshot,
            defaults={
                "prev_snapshot_id": previous.id,
                "changed_fields": changes.changed_fields,
                "event_types": changes.event_types,
                "deltas": changes.deltas,
                "magnitude": changes.magnitude,
                "merge_key": f"{target.id}:{now:%Y%m%d%H}",
                "detected_at": now,
            },
        )

    analyzed = 0
    if changes.significant:
        for tenant_id, self_product_id in fanout_tenants(target.id):
            job = enqueue(
                "analyze",
                {"event_id": event.id, "tenant_id": tenant_id, "self_product_id": self_product_id},
                priority=10 if _is_core(tenant_id, target.id) else 100,
                idempotency_key=(
                    f"analyze:{event.id}:{tenant_id}:{self_product_id or '-'}:{snapshot.id}"
                ),
            )
            if job is not None:
                analyzed += 1

    log.info(
        "collect.changed",
        extra={
            "target_id": target.id,
            "event_id": event.id,
            "fields": changes.changed_fields,
            "magnitude": str(changes.magnitude),
            "analyze_jobs": analyzed,
        },
    )
    return {
        "snapshot": snapshot.evidence_id,
        "changed": True,
        "event_id": event.id,
        "created": created,
        "analyze_jobs": analyzed,
    }


def _is_core(tenant_id: str, target_id: int) -> bool:
    from tenancy.models import TenantTarget

    return TenantTarget.all_objects.filter(
        tenant_id=tenant_id, target_id=target_id, priority=TenantTarget.PRIORITY_CORE, enabled=True
    ).exists()


def handle_schedule_tick(payload: dict) -> dict:
    """调度 tick：取到期 target → 扣预算 → 推进 due（先推进后投递）→ 投递采集任务。"""
    from django.db import transaction

    limit = int(payload.get("limit", 200))
    now = timezone.now()

    with transaction.atomic():
        due = list(
            Target.objects.filter(status=Target.STATUS_ACTIVE)
            .filter(
                Q(circuit_state=Target.CIRCUIT_CLOSED, next_due_at__lte=now)
                | Q(circuit_state=Target.CIRCUIT_HALF_OPEN, next_due_at__lte=now)
                # 冷却到期的熔断目标要重新进调度，否则一旦熔断就永久不再采集
                | Q(circuit_state=Target.CIRCUIT_OPEN, circuit_until__lte=now)
            )
            .order_by("next_due_at")
            .select_for_update(skip_locked=True)[:limit]
        )
        if not due:
            return {"due": 0, "enqueued": 0}

        # 按可用额度分批放行：拿不到预算的目标保持 due，下一轮再来，不会饿死也不会爆发
        allowance = budget.acquire_up_to(len(due))
        if allowance <= 0:
            return {"due": len(due), "enqueued": 0, "budget_exhausted": True}
        due = due[:allowance]

        enqueued = 0
        for target in due:
            slot = target.next_due_at
            update_fields = ["next_due_at"]
            if target.circuit_state == Target.CIRCUIT_OPEN:
                # 半开：只放一次探针，成功则恢复、失败则继续熔断（冷却时间会翻倍）。
                # 这个状态必须落库，否则每轮都会重复放探针。
                target.circuit_state = Target.CIRCUIT_HALF_OPEN
                update_fields.append("circuit_state")
            # 先推进 next_due_at —— 最多漏采一次，绝不重复采（合规上唯一正确的取向）
            jitter_seconds = int(_interval(target).total_seconds() * 0.1)
            offset = (target.id % 21 - 10) / 10 * jitter_seconds
            target.next_due_at = now + _interval(target) + timedelta(seconds=offset)
            target.save(update_fields=update_fields)
            job = enqueue(
                "collect",
                {"target_id": target.id, "due_slot": slot.isoformat()},
                idempotency_key=f"collect:{target.id}:{slot.isoformat()}",
            )
            if job is not None:
                enqueued += 1

    return {"due": len(due), "enqueued": enqueued}
