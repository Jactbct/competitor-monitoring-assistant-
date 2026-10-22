"""界面小工具：模板过滤器。

卡片里的 deadline / captured_at 存的是 ISO 字符串（JSON 字段，不是 DateTimeField），
不能直接用 Django 的 ``date`` 过滤器。这里统一解析并转本地时区，
否则界面上会出现 ``2026-10-23T05:26`` 这种只有机器爱看的格式。
"""

from __future__ import annotations

from datetime import datetime

from django import template
from django.utils import timezone
from django.utils.formats import date_format

register = template.Library()


@register.filter
def human_dt(value, fmt: str = "m-d H:i") -> str:
    """ISO 字符串 / datetime → 本地时区的 "月-日 时:分"。解析不了就原样截断，不抛错。"""
    if value in (None, ""):
        return "—"
    parsed: datetime | None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)[:16]
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return date_format(timezone.localtime(parsed), fmt)
