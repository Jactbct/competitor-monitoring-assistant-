"""通知渠道：企业微信 webhook / 邮件 / 站内（降级兜底）。

SPOF-4 降级链：企微 → 邮件 → 站内红点。任何一环失败都**不静默**，落 notification 表。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from core.config import get_settings
from core.log import get_logger

__all__ = ["DeliveryResult", "send_email_message", "send_site_message", "send_wecom_markdown"]

log = get_logger("notify")


@dataclass
class DeliveryResult:
    channel: str
    ok: bool
    detail: str = ""


def send_wecom_markdown(title: str, markdown: str) -> DeliveryResult:
    settings = get_settings()
    webhook = settings.notify.wecom_webhook
    if not webhook:
        return DeliveryResult("wecom", False, "未配置 WECOM_WEBHOOK")
    payload = {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{markdown}"}}
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.post(webhook, json=payload)
        if response.status_code != 200:
            return DeliveryResult("wecom", False, f"HTTP {response.status_code}")
        body = response.json()
        if body.get("errcode", 0) != 0:
            return DeliveryResult("wecom", False, f"errcode={body.get('errcode')} {body.get('errmsg')}")
        return DeliveryResult("wecom", True)
    except httpx.HTTPError as exc:
        return DeliveryResult("wecom", False, type(exc).__name__)


def send_email_message(subject: str, body_text: str, to: list[str]) -> DeliveryResult:
    settings = get_settings()
    if not settings.notify.mail_from or not to:
        return DeliveryResult("email", False, "未配置发件人或收件人")
    from django.core.mail import send_mail

    try:
        sent = send_mail(subject, body_text, settings.notify.mail_from, to, fail_silently=False)
        return DeliveryResult("email", bool(sent), "" if sent else "未发送")
    except Exception as exc:  # SMTP 的异常类型太杂，统一降级
        return DeliveryResult("email", False, type(exc).__name__)


def send_site_message(tenant_id: str, suggestion_id: str) -> DeliveryResult:
    """站内兜底：不需要外部依赖，前端轮询即可看到。"""
    return DeliveryResult("site", True, f"tenant={tenant_id} suggestion={suggestion_id}")
