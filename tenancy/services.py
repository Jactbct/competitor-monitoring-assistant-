"""租户层服务：跨模块唯一允许被调用的入口（架构文档 §5.2「跨模块只允许调用 services.py」）。

views 只做「取参 → 调这里 → 渲染」；worker 也调这里。所有写操作留审计。
"""

from __future__ import annotations

from datetime import timedelta

from django.db import DatabaseError, transaction
from django.db.models import Avg, Count
from django.utils import timezone

from core.db import tenant_atomic
from core.errors import AppError, ErrorCode
from core.log import get_logger
from core.tenant import current_tenant_id, tenant_scope
from domain.collect.guardrails import GuardrailError, normalize_url, url_hash, validate_url
from shared.models import ChangeEvent, Snapshot, Target, TargetDemand
from tenancy.models import (
    AuditLog,
    EventPriority,
    Feedback,
    SelfProduct,
    Suggestion,
    Task,
    Tenant,
    TenantTarget,
)

__all__ = [
    "accept_suggestion",
    "add_target",
    "audit",
    "collection_health",
    "delete_binding",
    "export_suggestions_csv",
    "fanout_tenants",
    "ignore_suggestion",
    "list_bindings",
    "list_suggestions",
    "mark_false_positive",
    "recompute_demand",
    "set_priority",
    "suggestion_feed_counts",
    "target_detail",
    "workbench_summary",
]

log = get_logger("tenancy")


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #
def audit(
    action: str,
    *,
    tenant_id: str | None = None,
    actor: str = "",
    target_type: str = "",
    target_id: str = "",
    detail: dict | None = None,
    ip: str = "",
    request_id: str = "",
) -> None:
    """写审计。

    两个坑（都是只有用受 RLS 约束的账号连真库才会暴露的）：

    1. **租户与当前上下文不一致时**（典型场景：登录成功后立刻写审计，此时中间件看到的
       还是匿名用户、上下文为空），RLS 的 ``WITH CHECK`` 会把这条插入拒掉 → 500。
       所以这里显式给这条写入单独套一次租户上下文。
    2. **没有租户的全局事件**（匿名登录失败、平台级操作）根本不该进租户审计表；
       落库失败时退化为结构化日志，绝不因为「记不了审计」把业务请求搞崩。
    """
    tid = tenant_id or current_tenant_id()
    payload = {
        "actor": actor,
        "action": action,
        "target_type": target_type,
        "target_id": str(target_id),
        "detail": detail or {},
        "ip": ip,
        "request_id": request_id,
    }
    if tid is None:
        log.info("audit.global", extra=payload)
        return
    try:
        if current_tenant_id() != str(tid):
            with tenant_scope(str(tid)), tenant_atomic(str(tid)):
                AuditLog.objects.create(tenant=tid, **payload)
        else:
            AuditLog.objects.create(tenant=tid, **payload)
    except DatabaseError:  # 审计不可写时不能连坐业务请求
        log.warning("audit.write_failed", extra={"action": action, "tenant_id": str(tid)})


# --------------------------------------------------------------------------- #
# 监控对象
# --------------------------------------------------------------------------- #
def recompute_demand(target_id: int) -> None:
    """共享层需求投影：取所有启用该 target 的租户周期的最小值（最高频者决定）。

    **不存租户数量**，否则可被反推成「监控热度榜」（补充文档 C-2）。
    """
    bindings = TenantTarget.all_objects.filter(target_id=target_id, enabled=True)
    if not bindings.exists():
        TargetDemand.objects.update_or_create(
            target_id=target_id, defaults={"has_monitor": False, "min_interval_min": 1440}
        )
        return
    intervals = [b.interval_min for b in bindings]
    min_interval = min(intervals)
    TargetDemand.objects.update_or_create(
        target_id=target_id, defaults={"has_monitor": True, "min_interval_min": min_interval}
    )
    Target.objects.filter(pk=target_id).update(interval_min=min_interval)


def fanout_tenants(target_id: int) -> list[tuple[str, str | None]]:
    """返回监控该目标的 (tenant_id, self_product_id) 组合，供分析任务扇出。"""
    rows = TenantTarget.all_objects.filter(target_id=target_id, enabled=True).values_list(
        "tenant_id", "self_product_id"
    )
    return [(str(tenant_id), str(sp) if sp else None) for tenant_id, sp in rows]


def add_target(
    tenant: Tenant,
    *,
    url: str,
    priority: str = TenantTarget.PRIORITY_NORMAL,
    self_product: SelfProduct | None = None,
    user=None,
    actor: str = "",
) -> TenantTarget:
    try:
        validate_url(normalize_url(url))
        normalized = normalize_url(url)
    except GuardrailError as exc:
        raise AppError.of(ErrorCode.TARGET_INVALID_URL, str(exc)) from exc

    current = TenantTarget.all_objects.filter(tenant=tenant, enabled=True).count()
    if current >= tenant.target_quota:
        raise AppError.of(
            ErrorCode.TARGET_QUOTA_EXCEEDED,
            f"监控对象已达当前套餐上限（{tenant.target_quota}）",
            limit=tenant.target_quota,
            current=current,
            plan=tenant.plan,
        )

    with transaction.atomic():
        target = Target.objects.filter(platform="taobao", url_hash=url_hash(normalized)).first()
        if target is None:
            target = Target.objects.create(
                platform="taobao",
                url=normalized,
                url_hash=url_hash(normalized),
                source="public_sample",
                next_due_at=timezone.now(),
                interval_min=TenantTarget.PRIORITY_BY_INTERVAL.get(priority, 720),
            )
        duplicate = TenantTarget.all_objects.filter(
            tenant=tenant, target_id=target.id, self_product=self_product
        ).first()
        if duplicate is not None and duplicate.enabled:
            raise AppError.of(ErrorCode.TARGET_DUPLICATED, "该链接已在监控列表中")
        if duplicate is not None:
            duplicate.enabled = True
            duplicate.priority = priority
            duplicate.save(update_fields=["enabled", "priority"])
            binding = duplicate
        else:
            binding = TenantTarget.objects.create(
                tenant=tenant,
                target_id=target.id,
                self_product=self_product,
                priority=priority,
                created_by=user.id if user else None,
            )
        recompute_demand(target.id)

    audit(
        "target.added",
        tenant_id=str(tenant.id),
        actor=actor,
        target_type="target",
        target_id=target.id,
        detail={"url": normalized, "priority": priority},
    )
    return binding


def set_priority(binding: TenantTarget, priority: str, *, actor: str = "") -> None:
    if priority not in dict(TenantTarget.PRIORITY_CHOICES):
        raise AppError.of(ErrorCode.COMMON_VALIDATION_FAILED, f"未知优先级：{priority}")
    binding.priority = priority
    binding.save(update_fields=["priority"])
    recompute_demand(binding.target_id)
    audit(
        "target.priority_changed",
        tenant_id=str(binding.tenant_id),
        actor=actor,
        target_type="tenant_target",
        target_id=binding.id,
        detail={"priority": priority},
    )


def delete_binding(binding: TenantTarget, *, actor: str = "") -> None:
    """软删：保留历史，快照与事件不删（FR-11）。"""
    binding.enabled = False
    binding.save(update_fields=["enabled"])
    recompute_demand(binding.target_id)
    audit(
        "target.removed",
        tenant_id=str(binding.tenant_id),
        actor=actor,
        target_type="tenant_target",
        target_id=binding.id,
    )


# --------------------------------------------------------------------------- #
# 建议闭环
# --------------------------------------------------------------------------- #
def accept_suggestion(suggestion: Suggestion, *, user, actor: str = "") -> Task:
    with transaction.atomic():
        suggestion.status = Suggestion.STATUS_ACCEPTED
        suggestion.save(update_fields=["status"])
        card = suggestion.card or {}
        task = Task.objects.create(
            tenant_id=suggestion.tenant_id,
            suggestion=suggestion,
            title=str(card.get("action") or "执行建议")[:120],
            detail=str(card.get("acceptance") or ""),
            owner=user,
            status=Task.STATUS_TODO,
            due_at=_parse_dt(card.get("deadline")),
        )
        Feedback.objects.create(
            tenant_id=suggestion.tenant_id,
            suggestion=suggestion,
            verdict=Feedback.VERDICT_ACCEPTED,
            user=user,
        )
    audit(
        "suggestion.accepted",
        tenant_id=str(suggestion.tenant_id),
        actor=actor,
        target_type="suggestion",
        target_id=suggestion.id,
        detail={"task_id": str(task.id)},
    )
    return task


IGNORE_REASON_TAGS = ("价格无压力", "我方策略不同", "竞品数据不准", "已在处理", "其他")


def ignore_suggestion(suggestion: Suggestion, *, user, reason_tag: str, note: str = "", actor: str = "") -> None:
    if not reason_tag:
        raise AppError.of(ErrorCode.COMMON_VALIDATION_FAILED, "忽略必须填写原因标签（FR-49）")
    suggestion.status = Suggestion.STATUS_IGNORED
    suggestion.save(update_fields=["status"])
    Feedback.objects.create(
        tenant_id=suggestion.tenant_id,
        suggestion=suggestion,
        verdict=Feedback.VERDICT_IGNORED,
        reason_tag=reason_tag,
        note=note,
        user=user,
    )
    audit(
        "suggestion.ignored",
        tenant_id=str(suggestion.tenant_id),
        actor=actor,
        target_type="suggestion",
        target_id=suggestion.id,
        detail={"reason_tag": reason_tag},
    )


def mark_false_positive(suggestion: Suggestion, *, user, note: str = "", actor: str = "") -> None:
    suggestion.status = Suggestion.STATUS_FALSE_POSITIVE
    suggestion.save(update_fields=["status"])
    Feedback.objects.create(
        tenant_id=suggestion.tenant_id,
        suggestion=suggestion,
        verdict=Feedback.VERDICT_FALSE_POSITIVE,
        note=note,
        user=user,
    )
    audit(
        "suggestion.false_positive",
        tenant_id=str(suggestion.tenant_id),
        actor=actor,
        target_type="suggestion",
        target_id=suggestion.id,
    )


def _parse_dt(value) -> object:
    if not value:
        return None
    from datetime import datetime

    from django.utils import timezone as dj_timezone

    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return dj_timezone.make_aware(parsed, dj_timezone.get_current_timezone())
    return parsed


# --------------------------------------------------------------------------- #
# 工作台聚合（首屏 4 模块）
# --------------------------------------------------------------------------- #
def workbench_summary(tenant_id: str) -> dict:
    now = timezone.now()
    week_ago = now - timedelta(days=7)

    suggestions = Suggestion.all_objects.filter(tenant_id=tenant_id)
    open_suggestions = suggestions.filter(status=Suggestion.STATUS_PENDING)
    p0 = open_suggestions.filter(priority="P0").count()
    p1 = open_suggestions.filter(priority="P1").count()
    p2 = open_suggestions.filter(priority="P2").count()

    tasks_todo = Task.all_objects.filter(tenant_id=tenant_id, status__in=[Task.STATUS_TODO, Task.STATUS_DOING])
    overdue = tasks_todo.filter(due_at__lt=now).count()

    bindings = TenantTarget.all_objects.filter(tenant_id=tenant_id, enabled=True)
    target_ids = list(bindings.values_list("target_id", flat=True))
    targets = Target.objects.filter(id__in=target_ids)
    healthy = targets.filter(status="active", circuit_state="closed").count()
    open_circuit = targets.filter(circuit_state="open").count()
    stale = targets.filter(last_captured_at__lt=now - timedelta(hours=6)).count() if target_ids else 0

    # 价格竞争力：我方售价 vs 竞品到手价（不确定的按展示价），低于我方记为压力
    products = SelfProduct.all_objects.filter(tenant_id=tenant_id)
    prices = [p for p in products.values_list("price_display", flat=True) if p]
    comp_prices = [
        p
        for p in targets.values_list("last_price_final", "last_price_display")
        for p in [p[0] or p[1]]
        if p
    ]
    competitiveness = None
    if prices and comp_prices:
        our_avg = sum(prices) / len(prices)
        comp_avg = sum(comp_prices) / len(comp_prices)
        if our_avg > 0:
            competitiveness = round(float((comp_avg - our_avg) / our_avg) * 100, 1)

    week_suggestions = suggestions.filter(created_at__gte=week_ago)
    total_week = week_suggestions.count()
    accepted_week = week_suggestions.filter(status=Suggestion.STATUS_ACCEPTED).count()
    degraded_week = week_suggestions.filter(degraded=True).count()

    recent = list(
        open_suggestions.order_by("-created_at")[:5].values(
            "id", "kind", "priority", "card", "created_at", "degraded"
        )
    )
    for item in recent:
        item["id"] = str(item["id"])
        item["created_at"] = item["created_at"].isoformat()

    return {
        "p0": p0,
        "p1": p1,
        "p2": p2,
        "tasks_todo": tasks_todo.count(),
        "overdue": overdue,
        "targets_total": len(target_ids),
        "targets_healthy": healthy,
        "targets_stale": stale,
        "circuit_open": open_circuit,
        "competitiveness": competitiveness,
        "week_total": total_week,
        "week_accepted": accepted_week,
        "week_degraded": degraded_week,
        "accept_rate": round(accepted_week / total_week * 100, 1) if total_week else None,
        "recent": recent,
        "generated_at": now.isoformat(),
    }


def collection_health(days: int = 7) -> dict:
    """平台运营视图：**只含运行指标**，不含任何租户业务内容。"""
    since = timezone.now() - timedelta(days=days)
    total = Snapshot.objects.filter(captured_at__gte=since).count()
    abnormal = Snapshot.objects.filter(captured_at__gte=since, status=Snapshot.STATUS_ABNORMAL).count()
    by_type = (
        Snapshot.objects.filter(captured_at__gte=since, status=Snapshot.STATUS_ABNORMAL)
        .values("abnormal_type")
        .annotate(n=Count("id"))
        .order_by("-n")[:8]
    )
    events = ChangeEvent.objects.filter(detected_at__gte=since).count()
    return {
        "snapshots": total,
        "abnormal": abnormal,
        "abnormal_rate": round(abnormal / total * 100, 2) if total else 0.0,
        "abnormal_by_type": [{"type": row["abnormal_type"] or "unknown", "n": row["n"]} for row in by_type],
        "events": events,
        "targets_active": Target.objects.filter(status="active").count(),
        "targets_circuit_open": Target.objects.filter(circuit_state="open").count(),
        "avg_completeness": float(
            Snapshot.objects.filter(captured_at__gte=since).aggregate(v=Avg("field_completeness"))["v"] or 0
        ),
    }


# --------------------------------------------------------------------------- #
# 页面用查询（views 只取结果，不做业务判断）
# --------------------------------------------------------------------------- #
def list_bindings(tenant_id: str, *, priority: str = "", keyword: str = "") -> list[dict]:
    """监控列表：一次查出绑定 + 目标投影，避免 N+1。"""
    bindings = TenantTarget.all_objects.filter(tenant_id=tenant_id, enabled=True)
    if priority:
        bindings = bindings.filter(priority=priority)
    rows = list(bindings.order_by("-created_at"))
    target_map = {
        t.id: t for t in Target.objects.filter(id__in=[b.target_id for b in rows])
    }
    result: list[dict] = []
    for binding in rows:
        target = target_map.get(binding.target_id)
        if target is None:
            continue
        if keyword and keyword not in (target.last_title or "") and keyword not in target.url:
            continue
        result.append(
            {
                "binding": binding,
                "target": target,
                "interval_min": binding.interval_min,
                "priority_label": dict(TenantTarget.PRIORITY_CHOICES).get(binding.priority, binding.priority),
            }
        )
    return result


def target_detail(tenant_id: str, binding: TenantTarget) -> dict:
    target = Target.objects.filter(pk=binding.target_id).first()
    if target is None:
        raise AppError.of(ErrorCode.TARGET_NOT_FOUND, "监控对象不存在")
    snapshots = list(
        Snapshot.objects.filter(target=target).order_by("-captured_at")[:12]
    )
    events = list(
        ChangeEvent.objects.filter(target=target).order_by("-detected_at")[:20]
    )
    priorities = dict(
        EventPriority.all_objects.filter(tenant_id=tenant_id, event_id__in=[e.id for e in events]).values_list(
            "event_id", "priority"
        )
    )
    for event in events:
        event.priority_label = priorities.get(event.id, "")
    latest = snapshots[0] if snapshots else None
    return {
        "target": target,
        "binding": binding,
        "snapshots": snapshots,
        "events": events,
        "latest": latest,
        "price_detail": (latest.fields.get("price_detail") if latest else None) or {},
        "attrs": (latest.fields.get("attrs") if latest else {}) or {},
    }


def list_suggestions(tenant_id: str, *, priority: str = "", status: str = "pending", kind: str = "") -> list:
    query = Suggestion.all_objects.filter(tenant_id=tenant_id)
    if status:
        query = query.filter(status=status)
    if priority:
        query = query.filter(priority=priority)
    if kind:
        query = query.filter(kind=kind)
    rows = list(query.order_by("priority", "-created_at")[:200])
    target_ids = {r.target_id for r in rows if r.target_id}
    titles = dict(Target.objects.filter(id__in=target_ids).values_list("id", "last_title"))
    for row in rows:
        row.target_title = titles.get(row.target_id) or "竞品商品"
    return rows


def suggestion_feed_counts(tenant_id: str) -> dict:
    base = Suggestion.all_objects.filter(tenant_id=tenant_id)
    pending = base.filter(status=Suggestion.STATUS_PENDING)
    return {
        "pending": pending.count(),
        "p0": pending.filter(priority="P0").count(),
        "p1": pending.filter(priority="P1").count(),
        "p2": pending.filter(priority="P2").count(),
        "accepted": base.filter(status=Suggestion.STATUS_ACCEPTED).count(),
        "ignored": base.filter(status=Suggestion.STATUS_IGNORED).count(),
        "false_positive": base.filter(status=Suggestion.STATUS_FALSE_POSITIVE).count(),
    }


def _csv_safe(value) -> object:
    """防 CSV 公式注入：Excel/WPS 会把 = + - @ 开头的单元格当公式执行。"""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def export_suggestions_csv(rows, *, tenant_name: str, actor: str) -> str:
    """带水印的 CSV：表头写入租户名/操作人/导出时间/范围，并留审计（§10.4）。"""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    exported_at = timezone.localtime()
    writer.writerow(
        ["# 导出水印", _csv_safe(tenant_name), _csv_safe(actor), f"{exported_at:%Y-%m-%d %H:%M:%S}", f"{len(rows)} 条"]
    )
    writer.writerow(["优先级", "类型", "状态", "结论", "执行动作", "责任人", "截止时间", "预期影响", "验收标准", "创建时间"])
    for row in rows:
        card = row.card or {}
        writer.writerow(
            [
                _csv_safe(v)
                for v in (
                    row.priority,
                    row.kind,
                    row.status,
                    card.get("conclusion", ""),
                    card.get("action", ""),
                    card.get("owner_role", ""),
                    str(card.get("deadline", ""))[:16],
                    card.get("expected_impact", ""),
                    card.get("acceptance", ""),
                    f"{row.created_at:%Y-%m-%d %H:%M:%S}",
                )
            ]
        )
    audit(
        "suggestion.exported",
        tenant_id=str(rows[0].tenant_id) if rows else current_tenant_id(),
        actor=actor,
        detail={"rows": len(rows), "watermark": True},
    )
    return buffer.getvalue()
