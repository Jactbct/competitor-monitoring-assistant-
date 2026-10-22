"""自检与指标：单人版可观测（架构文档 §8.4）。

不引入 Prometheus + Grafana（两个组件 + 一套查询语言，维护成本高于收益），
改为「一张指标表 + 一封每日邮件 + 一个外部探针」。
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Avg
from django.utils import timezone

from core.config import get_settings
from core.log import get_logger
from infra import storage
from infra.notify import send_email_message, send_wecom_markdown
from jobs.queue import purge_finished, queue_stats
from shared.maintenance import purge_snapshots, snapshot_volume
from shared.models import ChangeEvent, Snapshot, Target
from tenancy.models import (
    AppUser,
    AuditLog,
    MetricDaily,
    Suggestion,
    Tenant,
)
from tenancy.services import collection_health

__all__ = ["handle_daily_digest", "handle_health_snapshot", "handle_silence_check"]

log = get_logger("health")


def _put(day, hour: int, metric: str, value: float, dim: dict | None = None) -> None:
    MetricDaily.objects.update_or_create(
        day=day, hour=hour, metric=metric, dim=dim or {}, defaults={"value": float(value)}
    )


def handle_health_snapshot(payload: dict) -> dict:
    """每小时跑一次：把关键计数写进指标表，工作台 /ops/health 直接读表画图。"""
    now = timezone.localtime()
    day, hour = now.date(), now.hour
    since = timezone.now() - timedelta(hours=1)

    hourly = Snapshot.objects.filter(captured_at__gte=since)
    total = hourly.count()
    abnormal = hourly.filter(status=Snapshot.STATUS_ABNORMAL).count()
    _put(day, hour, "snapshots", total)
    _put(day, hour, "snapshots_abnormal", abnormal)
    _put(day, hour, "abnormal_rate", (abnormal / total) if total else 0)

    events = ChangeEvent.objects.filter(detected_at__gte=since).count()
    _put(day, hour, "change_events", events)

    # 到手价可确定率：B 类（跟价）闸门的核心判据（P-1：≥70% 才允许自动推送）
    valid = Snapshot.objects.filter(captured_at__gte=since, status=Snapshot.STATUS_VALID)
    certain = valid.filter(price_confidence="certain").count()
    valid_total = valid.count()
    _put(day, hour, "price_certain", certain)
    _put(day, hour, "price_valid", valid_total)
    _put(day, hour, "price_certain_ratio", (certain / valid_total) if valid_total else 0)

    stats = queue_stats()
    for key, value in stats.items():
        _put(day, hour, f"queue_{key}", value)

    suggestions = Suggestion.all_objects.filter(created_at__gte=since)
    sug_total = suggestions.count()
    _put(day, hour, "suggestions", sug_total)
    degraded = suggestions.filter(degraded=True).count()
    _put(day, hour, "degraded", degraded)
    _put(day, hour, "degraded_ratio", (degraded / sug_total) if sug_total else 0)

    blocked = AuditLog.objects.filter(action="tenant.cross_access_blocked", created_at__gte=since).count()
    _put(day, hour, "cross_tenant_blocked", blocked)
    if blocked > 0:
        log.error("isolation.breach_detected", extra={"count": blocked})

    _put(day, hour, "circuit_open_targets", Target.objects.filter(circuit_state="open").count())
    _put(
        day,
        hour,
        "completeness_avg",
        float(hourly.aggregate(v=Avg("field_completeness"))["v"] or 0),
    )
    return {"hour": str(hour), "snapshots": total, "abnormal": abnormal, "queue": stats}


def handle_silence_check(payload: dict) -> dict:
    """沉默告警：有活跃目标但 6 小时零变化事件 —— 大概率是解析失效（R-A2）。"""
    now = timezone.now()
    active = Target.objects.filter(status="active").count()
    if active == 0:
        return {"skipped": "no_active_target"}
    recent = ChangeEvent.objects.filter(detected_at__gte=now - timedelta(hours=6)).count()
    if recent > 0:
        return {"events_6h": recent, "alert": False}
    log.error("health.silence_alert", extra={"active_targets": active, "events_6h": 0})
    return {"events_6h": 0, "alert": True, "active_targets": active}


def handle_daily_digest(payload: dict) -> dict:
    """每日 09:00 一条邮件 = 全部值班信息；顺便做保留期清理。"""
    settings = get_settings()
    health = collection_health(days=1)
    stats = queue_stats()
    since = timezone.now() - timedelta(days=1)

    degrad = Suggestion.all_objects.filter(created_at__gte=since)
    total = degrad.count()
    degraded = degrad.filter(degraded=True).count()
    manual = degrad.filter(quality_gate="manual_required").count()
    p0 = degrad.filter(priority="P0").count()

    lines = [
        f"环境：{settings.env}　采集模式：{settings.collector.mode}　出口预算：{settings.collector.qps} QPS",
        "",
        f"采集：{health['snapshots']} 次，异常 {health['abnormal']} 次（{health['abnormal_rate']}%）",
        f"变化事件：{health['events']}　熔断目标：{health['targets_circuit_open']}",
        f"字段完整率均值：{health['avg_completeness']:.3f}",
        f"队列：待处理 {stats['pending']}，积压>5min {stats['backlog_5m']}，死信 {stats['dead']}",
        f"建议：共 {total}，P0 {p0}，降级 {degraded}，需人工确认 {manual}",
        f"越权拦截：{AuditLog.objects.filter(action='tenant.cross_access_blocked', created_at__gte=since).count()}",
        "",
        "异常 Top：",
    ]
    for row in health["abnormal_by_type"]:
        lines.append(f"  - {row['type']}: {row['n']}")
    if not health["abnormal_by_type"]:
        lines.append("  - 无")

    purged_raw = storage.purge_expired()
    purged_jobs = purge_finished()
    purged_snap = purge_snapshots(settings.snapshot_retention_days)
    volume = snapshot_volume()
    lines += [
        "",
        f"清理：原始页 {purged_raw} 个，历史任务 {purged_jobs} 条，"
        f"过期快照 {purged_snap['snapshots']} 条（保留 {settings.snapshot_retention_days} 天）",
        f"快照表：{volume['rows']} 行，最早 {str(volume['oldest'])[:10] or '—'}",
    ]

    body = "\n".join(lines)
    # 正文含全平台聚合（各租户建议数、队列、越权拦截），只能发给平台运营；
    # 租户管理员要看自己的数据，走「设置页」的用量与建议列表。
    admins = list(
        AppUser.objects.filter(role=AppUser.ROLE_PLATFORM_OPS, status="active")
        .exclude(email="")
        .values_list("email", flat=True)[:20]
    )
    wecom = send_wecom_markdown("每日采集与建议自检", body)
    email = send_email_message("【竞品监控】每日自检", body, admins) if admins else None

    log.info("health.digest_sent", extra={"wecom": wecom.ok, "email": bool(email and email.ok)})
    return {
        "body": body,
        "wecom": wecom.__dict__,
        "email": email.__dict__ if email else None,
        "purged_raw": purged_raw,
        "purged_jobs": purged_jobs,
        "purged_snapshots": purged_snap,
        "snapshot_volume": volume,
        "tenants": Tenant.objects.count(),
    }
