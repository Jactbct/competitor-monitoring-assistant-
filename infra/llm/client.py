"""OpenAI 兼容协议的 LLM 适配器。

* 模型 ID 收敛到配置（``LLM_MODEL_EXTRACT`` / ``LLM_MODEL_REASON``），换模型不改代码；
* 结果缓存落库（``shared.llm_cache``），key = 内容哈希 + prompt 版本 + 模型；
* 日 token 配额**按租户**计算，超限**直接降级不重试**：一个租户打爆额度不能影响别人；
* 调用失败只返回异常给上层降级，不在这里吞掉。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from decimal import Decimal

import httpx

from core.config import Settings, get_settings
from core.log import get_logger
from domain.analyze.ports import LlmResult

__all__ = ["LlmError", "OpenAiCompatibleLlm", "get_llm"]

log = get_logger("llm")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)

#: 极简单价表（¥/1k tokens），只用于成本估算，不参与计费
_PRICE_TABLE = {
    "small": (Decimal("0.001"), Decimal("0.002")),
    "mid": (Decimal("0.004"), Decimal("0.008")),
}


class LlmError(RuntimeError):
    pass


class QuotaExceeded(LlmError):
    pass


class _FatalLlmError(LlmError):
    """不值得重试的错误（配置类）。对外仍然是 LlmError，上层降级逻辑不用改。"""


class OpenAiCompatibleLlm:
    """满足 :class:`domain.analyze.ports.LlmPort` 协议。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.llm = self.settings.llm

    # ---------------------------------------------------------------- cache
    @staticmethod
    def _cache_key(cache_material: str, prompt_version: str, model: str) -> str:
        raw = f"{cache_material}|{prompt_version}|{model}".encode()
        return hashlib.sha256(raw).hexdigest()

    def _read_cache(self, key: str) -> LlmResult | None:
        from shared.models import LlmCache

        row = LlmCache.objects.filter(cache_key=key).first()
        if row is None:
            return None
        return LlmResult(
            data=row.payload,
            model="cache",
            tokens_in=0,
            tokens_out=0,
            cost=Decimal("0"),
            cached=True,
            prompt_version=self.llm.prompt_version,
        )

    def _write_cache(self, key: str, node: str, result: LlmResult) -> None:
        from shared.models import LlmCache

        LlmCache.objects.update_or_create(
            cache_key=key,
            defaults={
                "node": node,
                "payload": result.data,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
            },
        )

    # ---------------------------------------------------------------- quota
    def _tokens_used_today(self, tenant_id: str | None) -> int:
        """配额口径是**每租户/日**。

        ``tenant_id`` 为空时不限（平台级运维调用）；业务路径上永远带租户。
        全局口径会让一个大客户把所有人的模型调用一起拖停，这是 SaaS 里最不该出现的故障。
        """
        if not tenant_id:
            return 0
        from django.db.models import Sum
        from django.utils import timezone

        from tenancy.models import LlmUsage

        rows = LlmUsage.all_objects.filter(tenant_id=tenant_id, day=timezone.localdate()).aggregate(
            tin=Sum("tokens_in"), tout=Sum("tokens_out")
        )
        return int((rows["tin"] or 0) + (rows["tout"] or 0))

    @staticmethod
    def _record_usage(
        tenant_id: str | None, node: str, tokens_in: int, tokens_out: int, cost: Decimal
    ) -> None:
        """按租户/天/节点累加用量。写失败不能影响分析结果 —— 用量是账，不是业务数据。"""
        if not tenant_id:
            return
        from django.db.models import F
        from django.utils import timezone

        from tenancy.models import LlmUsage

        day = timezone.localdate()
        row, _ = LlmUsage.all_objects.get_or_create(tenant_id=tenant_id, day=day, node=node)
        LlmUsage.all_objects.filter(pk=row.pk).update(
            calls=F("calls") + 1,
            tokens_in=F("tokens_in") + tokens_in,
            tokens_out=F("tokens_out") + tokens_out,
            cost=F("cost") + cost,
        )

    @staticmethod
    def _record_tokens(tokens: int) -> None:
        from django.db.models import F
        from django.utils import timezone

        from tenancy.models import MetricDaily

        now = timezone.localtime()
        row, _ = MetricDaily.objects.get_or_create(
            day=now.date(), hour=now.hour, metric="llm_tokens", dim={}, defaults={"value": 0}
        )
        MetricDaily.objects.filter(pk=row.pk).update(value=F("value") + tokens)

    # ---------------------------------------------------------------- call
    def complete_json(
        self,
        *,
        node: str,
        system: str,
        user: str,
        model: str,
        cache_material: str,
        tenant_id: str | None = None,
    ) -> LlmResult:
        if not self.llm.enabled:
            raise LlmError("LLM 未启用（LLM_MODE=off 或缺少 API Key）")

        tier = self.llm.model_extract if node == "n1" else self.llm.model_reason
        model_name = tier or model
        key = self._cache_key(cache_material, self.llm.prompt_version, model_name)

        cached = self._read_cache(key)
        if cached is not None:
            return cached

        if self._tokens_used_today(tenant_id) >= self.llm.daily_token_quota > 0:
            raise QuotaExceeded("租户今日 LLM token 配额已用尽，降级为规则模式")

        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user[: self.llm.max_input_chars * 4]},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.llm.api_key}", "Content-Type": "application/json"}
        url = f"{self.llm.base_url.rstrip('/')}/chat/completions"

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                started = time.perf_counter()
                with httpx.Client(timeout=self.llm.timeout_s) as client:
                    response = client.post(url, json=payload, headers=headers)
                if response.status_code >= 500:
                    raise LlmError(f"上游 {response.status_code}")
                if response.status_code == 429:
                    raise LlmError(f"限流 429（Retry-After={response.headers.get('retry-after')}）")
                if response.status_code >= 400:
                    # 401/403/400（key 错、模型名错、不支持 JSON 模式）重试只会放大故障
                    raise _FatalLlmError(f"请求被拒绝 {response.status_code}: {response.text[:200]}")
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                data = json.loads(_FENCE_RE.sub("", content).strip())
                usage = body.get("usage", {})
                tokens_in = int(usage.get("prompt_tokens", 0))
                tokens_out = int(usage.get("completion_tokens", 0))
                price_in, price_out = _PRICE_TABLE.get(model_name, _PRICE_TABLE["mid"])
                cost = ((Decimal(tokens_in) / 1000) * price_in + (Decimal(tokens_out) / 1000) * price_out).quantize(
                    Decimal("0.0001")
                )
                result = LlmResult(
                    data=data,
                    model=model_name,
                    node=node,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost=cost,
                    prompt_version=self.llm.prompt_version,
                )
                # 记账是「账」，不是业务结果：缓存/指标/用量写失败只告警，不能把已付费的结果丢掉
                try:
                    self._write_cache(key, node, result)
                except Exception as exc:  # noqa: BLE001 - 记账失败不应中断业务
                    log.warning("llm.accounting_failed", extra={"step": "cache", "error": type(exc).__name__})
                try:
                    self._record_tokens(tokens_in + tokens_out)
                except Exception as exc:  # noqa: BLE001
                    log.warning("llm.accounting_failed", extra={"step": "metric", "error": type(exc).__name__})
                try:
                    self._record_usage(tenant_id, node, tokens_in, tokens_out, cost)
                except Exception as exc:  # noqa: BLE001
                    log.warning("llm.accounting_failed", extra={"step": "usage", "error": type(exc).__name__})
                log.info(
                    "llm.completed",
                    extra={"node": node, "model": model_name, "tokens_in": tokens_in,
                           "tokens_out": tokens_out, "duration_ms": int((time.perf_counter() - started) * 1000)},
                )
                return result
            except _FatalLlmError as exc:
                # 配置错误不重试，直接抛给上层降级
                log.warning("llm.fatal", extra={"node": node, "error": str(exc)[:200]})
                raise LlmError(str(exc)) from exc
            except (httpx.HTTPError, LlmError, KeyError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.5)

        log.warning("llm.failed", extra={"node": node, "error": type(last_error).__name__})
        raise LlmError(f"调用失败：{last_error}")


def get_llm() -> OpenAiCompatibleLlm | None:
    """未启用时返回 ``None``，上层按确定性规则路径运行（这不是异常）。"""
    settings = get_settings()
    if not settings.llm.enabled:
        return None
    return OpenAiCompatibleLlm(settings)
