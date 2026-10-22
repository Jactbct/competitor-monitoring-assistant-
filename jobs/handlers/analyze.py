"""分析 handler：把一条变化事件翻译成「一个租户的一条建议」。

注意租户上下文：worker 不在任何租户会话里，所以这里的读写一律用 ``all_objects``
并显式带 ``tenant_id``；写库前用 ``tenant_scope`` 绑定上下文，保证默认管理器
与审计的行为和 web 侧一致。
"""

from __future__ import annotations

import os
from decimal import Decimal

from django.utils import timezone

from core.config import get_settings
from core.db import tenant_atomic
from core.log import get_logger
from core.tenant import tenant_scope
from domain.analyze.decide import AnalysisContext, decide, should_analyze
from domain.analyze.prompt_loader import load_prompts
from domain.extract.promo import PromoItem
from infra.llm import LlmError, get_llm
from jobs.queue import enqueue
from shared.models import ChangeEvent, Snapshot
from tenancy.models import Analysis, EventPriority, SelfProduct, Suggestion, TenantTarget

__all__ = ["handle_analyze"]

log = get_logger("analyze")


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _promo_items(snapshot: Snapshot) -> list[PromoItem]:
    items: list[PromoItem] = []
    for raw in snapshot.promo_detail or []:
        if not isinstance(raw, dict):
            continue
        try:
            items.append(
                PromoItem(
                    activity_type=str(raw.get("activity_type") or "无"),
                    threshold_yuan=_dec(raw.get("threshold_yuan")),
                    discount_yuan=_dec(raw.get("discount_yuan")),
                    repeat_tiers=raw.get("repeat_tiers"),
                    sekill_price=_dec(raw.get("seckill_price")),
                    evidence_text=str(raw.get("evidence_text") or ""),
                    confidence=float(raw.get("confidence") or 1.0),
                )
            )
        except (TypeError, ValueError):
            continue
    return items


def _evidence(snapshot: Snapshot, previous: Snapshot | None) -> list[dict]:
    """证据回链：当前快照 + 前一快照各取一句原文，必须能点回快照详情。"""
    evidence: list[dict] = []
    for snap in (snapshot, previous):
        if snap is None:
            continue
        quote = ""
        blocks = snap.fields.get("evidence_blocks") or []
        if blocks:
            quote = str(blocks[0])
        if not quote:
            quote = str(snap.fields.get("title") or "")[:80]
        price = snap.fields.get("price_final") or snap.fields.get("price_display")
        if price:
            quote = f"{quote}；价格 {price}" if quote else f"价格 {price}"
        evidence.append(
            {
                "snapshot_evidence_id": snap.evidence_id,
                "quote": quote[:200],
                "captured_at": snap.captured_at.isoformat(),
            }
        )
    return evidence


def _b_class_enabled() -> bool:
    """P-1 闸门：M1 实测到手价可确定率 ≥70% 才打开 B 类默认推送（默认关闭）。"""
    return os.environ.get("B_CLASS_ENABLED", "0").strip() in {"1", "true", "yes", "on"}


def handle_analyze(payload: dict) -> dict:
    event_id = int(payload["event_id"])
    tenant_id = str(payload["tenant_id"])
    self_product_id = payload.get("self_product_id")
    self_product_id = str(self_product_id) if self_product_id else None

    event = ChangeEvent.objects.filter(pk=event_id).first()
    if event is None:
        return {"skipped": "event_missing"}

    snapshot = Snapshot.objects.filter(pk=event.snapshot_id).first()
    previous = (
        Snapshot.objects.filter(pk=event.prev_snapshot_id).first() if event.prev_snapshot_id else None
    )
    if snapshot is None:
        return {"skipped": "snapshot_missing"}

    binding = TenantTarget.all_objects.filter(
        tenant_id=tenant_id, target_id=event.target_id, self_product_id=self_product_id, enabled=True
    ).first()
    if binding is None:
        return {"skipped": "no_binding"}

    product = SelfProduct.all_objects.filter(pk=self_product_id, tenant_id=tenant_id).first() if self_product_id else None

    if not should_analyze(event.changed_fields, True):
        return {"skipped": "not_significant"}

    now = timezone.now()
    evidence = _evidence(snapshot, previous)
    cur_fields = snapshot.fields or {}
    prev_fields = (previous.fields if previous else {}) or {}

    ctx = AnalysisContext(
        now=now,
        tenant_priority=binding.priority,
        changed_fields=list(event.changed_fields or []),
        event_types=list(event.event_types or []),
        deltas=event.deltas or {},
        magnitude=event.magnitude,
        cur_fields=cur_fields,
        prev_fields=prev_fields,
        cur_price_final=snapshot.price_final,
        cur_price_display=snapshot.price_display,
        price_uncertain=(snapshot.price_confidence != "certain") or snapshot.price_final is None,
        our_sku=product.sku if product else "",
        our_title=(product.title if product else "") or "",
        our_attrs=(product.attrs if product else {}) or {},
        our_price=product.price_display if product else None,
        our_cost=product.cost if product else None,
        margin_floor=product.margin_floor if product else Decimal("0.22"),
        evidence=evidence,
        page_text=str(cur_fields.get("evidence_blocks") or ""),
        promo_items=_promo_items(snapshot),
        b_class_enabled=_b_class_enabled(),
        cache_material=f"{snapshot.content_sha256}:{event.id}:{tenant_id}:{self_product_id or '-'}",
        tenant_id=tenant_id,
    )

    llm = get_llm()
    prompts = None
    if llm is not None:
        try:
            prompts = load_prompts(get_settings().llm.prompt_version)
        except OSError as exc:
            # 提示词文件缺失/版本名写错：走确定性规则路径，绝不让分析任务重试到死
            log.warning("analyze.prompt_missing", extra={"error": type(exc).__name__, "version": get_settings().llm.prompt_version})
            prompts = None
    try:
        decision = decide(ctx, llm, prompts)
    except LlmError as exc:
        log.warning("analyze.llm_degraded", extra={"error": type(exc).__name__})
        decision = decide(ctx, None, None)

    with tenant_scope(tenant_id), tenant_atomic(tenant_id):
        analysis, _ = Analysis.all_objects.update_or_create(
            tenant_id=tenant_id,
            event_id=event.id,
            self_product_id=self_product_id,
            defaults={
                "attribution": decision.attribution,
                "threat_level": decision.threat_level,
                "confidence": decision.confidence,
                "evidence": evidence,
                "model_meta": decision.model_meta,
                "degraded": decision.degraded,
            },
        )
        EventPriority.all_objects.update_or_create(
            tenant_id=tenant_id,
            event_id=event.id,
            self_product_id=self_product_id,
            defaults={"priority": decision.priority},
        )
        suggestion, created = Suggestion.all_objects.get_or_create(
            tenant_id=tenant_id,
            analysis=analysis,
            kind=decision.kind,
            defaults={
                "event_id": event.id,
                "target_id": event.target_id,
                "priority": decision.priority,
                "card": decision.card,
                "status": Suggestion.STATUS_PENDING,
                "quality_gate": decision.quality_gate,
                "degraded": decision.degraded,
            },
        )
        if not created:
            suggestion.card = decision.card
            suggestion.priority = decision.priority
            suggestion.quality_gate = decision.quality_gate
            suggestion.degraded = decision.degraded
            suggestion.save(update_fields=["card", "priority", "quality_gate", "degraded"])

        if decision.priority == "P0" and decision.quality_gate == "pass":
            # P0 立即推送，不进次日日报
            enqueue(
                "notify",
                {"suggestion_id": str(suggestion.id), "tenant_id": tenant_id},
                priority=5,
                idempotency_key=f"notify:{suggestion.id}:p0",
            )

    log.info(
        "analyze.completed",
        extra={
            "event_id": event.id,
            "tenant_id": tenant_id,
            "kind": decision.kind,
            "priority": decision.priority,
            "degraded": decision.degraded,
            "quality_gate": decision.quality_gate,
        },
    )
    return {
        "suggestion_id": str(suggestion.id),
        "kind": decision.kind,
        "priority": decision.priority,
        "degraded": decision.degraded,
        "quality_gate": decision.quality_gate,
    }
