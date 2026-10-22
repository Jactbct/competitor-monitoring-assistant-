"""通知 handler：P0 实时推送，降级链 企微 → 邮件 → 站内。

幂等键 ``(suggestion, channel, hour_bucket)`` 直接落在唯一约束上（FR-52：1 小时不重复）。
"""

from __future__ import annotations

from django.utils import timezone

from core.db import tenant_atomic
from core.log import get_logger
from core.tenant import tenant_scope
from infra.notify import send_email_message, send_site_message, send_wecom_markdown
from tenancy.models import AppUser, Notification, Suggestion, Tenant

__all__ = ["handle_notify", "render_markdown"]

log = get_logger("notify")


def render_markdown(card: dict) -> str:
    evidence = card.get("evidence") or []
    quote = evidence[0].get("quote", "") if evidence else ""
    cost = card.get("cost_risk") or {}
    lines = [
        f"**结论**：{card.get('conclusion', '')}",
        f"**动作**：{card.get('action', '')}",
        f"**责任人**：{card.get('owner_role', '')}　**截止**：{str(card.get('deadline', ''))[:16].replace('T', ' ')}",
        f"**预期影响**：{card.get('expected_impact', '')}",
    ]
    if cost.get("breakeven_volume_ratio"):
        lines.append(f"**打平销量倍数**：{cost['breakeven_volume_ratio']}（毛利 {cost.get('margin_before')} → {cost.get('margin_after')}）")
    if cost.get("below_floor"):
        lines.append("**注意**：跟价后毛利率低于底线，需审批")
    if quote:
        lines.append(f"**证据**：{quote[:120]}")
    lines.append(f"**验收**：{card.get('acceptance', '')}")
    return "\n".join(lines)


def handle_notify(payload: dict) -> dict:
    suggestion_id = str(payload["suggestion_id"])
    bucket = timezone.localtime().strftime("%Y%m%d%H")

    suggestion = Suggestion.all_objects.filter(pk=suggestion_id).first()
    if suggestion is None:
        return {"skipped": "suggestion_missing"}

    card = suggestion.card or {}
    tenant = Tenant.objects.filter(pk=suggestion.tenant_id).first()
    tenant_name = tenant.name if tenant else str(suggestion.tenant_id)
    title = f"[{suggestion.priority}] {tenant_name} 竞品变动"
    markdown = render_markdown(card)

    delivered: list[dict] = []
    with tenant_scope(str(suggestion.tenant_id)), tenant_atomic(str(suggestion.tenant_id)):
        # 站内始终有（兜底，也让工作台红点可靠）
        Notification.all_objects.get_or_create(
            tenant_id=suggestion.tenant_id,
            suggestion=suggestion,
            channel="site",
            bucket=bucket,
            defaults={"status": "sent"},
        )
        delivered.append(send_site_message(str(suggestion.tenant_id), str(suggestion.id)).__dict__)

        if Notification.all_objects.filter(
            tenant_id=suggestion.tenant_id, suggestion=suggestion, channel="wecom", bucket=bucket
        ).exists():
            return {"skipped": "duplicate", "delivered": delivered}

        result = send_wecom_markdown(title, markdown)
        if not result.ok and "未配置" not in result.detail:
            admins = list(
                AppUser.objects.filter(tenant_id=suggestion.tenant_id, role=AppUser.ROLE_ADMIN)
                .exclude(email="")
                .values_list("email", flat=True)[:3]
            )
            result = send_email_message(title, markdown.replace("**", ""), admins)

        if result.ok:
            Notification.all_objects.get_or_create(
                tenant_id=suggestion.tenant_id,
                suggestion=suggestion,
                channel=result.channel,
                bucket=bucket,
                defaults={"status": "sent"},
            )
            suggestion.push_at = timezone.now()
            suggestion.save(update_fields=["push_at"])
        elif "未配置" in result.detail:
            # 渠道未配置不是故障：站内已经兜底，仅记录一条便于排查
            Notification.all_objects.get_or_create(
                tenant_id=suggestion.tenant_id,
                suggestion=suggestion,
                channel=result.channel or "none",
                bucket=bucket,
                defaults={"status": "skipped", "error": result.detail},
            )
        else:
            Notification.all_objects.get_or_create(
                tenant_id=suggestion.tenant_id,
                suggestion=suggestion,
                channel=result.channel,
                bucket=bucket,
                defaults={"status": "failed", "error": result.detail},
            )
            log.warning("notify.failed", extra={"channel": result.channel, "detail": result.detail})

        delivered.append(result.__dict__)

    return {"delivered": delivered}
