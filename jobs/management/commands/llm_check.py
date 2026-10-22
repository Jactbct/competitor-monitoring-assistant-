"""模型连通性自检：填完 Key 后第一件该跑的命令。

为什么单独做一条命令：接第三方网关时，失败原因几乎总是这五种之一，
但报错信息都很含糊（401/404/400 混在一起）。这里把它们逐条翻成人话：

| 现象 | 真实原因 |
|---|---|
| 401 / 403 | Key 错、没带 Bearer、或该 Key 无权访问这个模型 |
| 404 | base_url 少了 `/v1`，或模型名不存在（先 `--list-models` 看有哪些） |
| 400 提到 response_format | 网关不完整支持 JSON 模式（客户端已会自动去掉该字段重试一次） |
| 返回内容不是 JSON | 模型不会按 JSON 输出，或提示词被网关改写 |
| 超时 | 网关慢，调 `LLM_TIMEOUT_S` |

用法：
    python manage.py llm_check --list-models     # 先看这个 Key 能用哪些模型
    python manage.py llm_check                   # 用当前配置跑一次真实调用
    python manage.py llm_check --model gpt-4o-mini
"""

from __future__ import annotations

import httpx
from django.core.management.base import BaseCommand, CommandError

from core.config import get_settings
from infra.llm.client import LlmError, QuotaExceeded, get_llm

HINTS = {
    401: "Key 不对或没带 Bearer（检查 LLM_API_KEY 是否有多余空格/引号）",
    403: "Key 无权访问该模型（换模型或找供应商开权限）",
    404: "base_url 少了 /v1，或模型名不存在（先跑 --list-models）",
    429: "被限流（稍后重试，或降 LLM_DAILY_TOKEN_QUOTA 之外的并发）",
}


class Command(BaseCommand):
    help = "检查 LLM 配置是否可用（列模型 / 跑一次真实调用）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--list-models", action="store_true", help="列出该 Key 可用的模型")
        parser.add_argument("--model", help="临时指定模型名（覆盖 LLM_MODEL_EXTRACT）")

    def handle(self, *args, **options) -> None:
        settings = get_settings()
        llm = settings.llm

        self.stdout.write("\n当前配置：")
        self.stdout.write(f"  LLM_MODE      = {llm.mode}")
        self.stdout.write(f"  LLM_BASE_URL  = {llm.base_url or '(空)'}")
        self.stdout.write(f"  LLM_API_KEY   = {'已设置（' + str(len(llm.api_key)) + ' 字符）' if llm.api_key else '(空)'}")
        self.stdout.write(f"  MODEL_EXTRACT = {llm.model_extract}")
        self.stdout.write(f"  MODEL_REASON  = {llm.model_reason}")
        self.stdout.write(f"  TIMEOUT       = {llm.timeout_s}s")

        if not llm.base_url:
            raise CommandError("LLM_BASE_URL 为空，先在 .env 里填写")
        if not llm.api_key:
            raise CommandError(
                "LLM_API_KEY 为空。打开项目根目录的 .env，把 LLM_API_KEY= 后面填上你的 Key（保存后重跑本命令）"
            )
        if llm.mode != "openai":
            raise CommandError(f"LLM_MODE={llm.mode}，要真实调用请设为 openai")

        if options["list_models"]:
            self._list_models(llm.base_url, llm.api_key)
            return

        if options["model"]:
            llm.model_extract = options["model"]
            llm.model_reason = options["model"]

        self._smoke(llm)

    def _list_models(self, base_url: str, api_key: str) -> None:
        url = f"{base_url.rstrip('/')}/models"
        try:
            with httpx.Client(timeout=15) as client:
                response = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        except httpx.HTTPError as exc:
            raise CommandError(f"连不上 {url}：{type(exc).__name__}") from exc

        if response.status_code >= 400:
            raise CommandError(
                f"{response.status_code} 从 {url} 取模型列表失败 —— "
                f"{HINTS.get(response.status_code, '该网关可能不提供 /models 接口，直接跑 llm_check 试一次调用')}"
            )

        body = response.json()
        rows = body.get("data") if isinstance(body, dict) else body
        if not isinstance(rows, list):
            raise CommandError(f"/models 返回结构不认识：{str(body)[:200]}")

        self.stdout.write(f"\n可用模型（{len(rows)} 个）：")
        for row in rows[:80]:
            model_id = row.get("id") if isinstance(row, dict) else str(row)
            self.stdout.write(f"  {model_id}")

    def _smoke(self, llm) -> None:
        client = get_llm()
        if client is None:
            raise CommandError("客户端未启用（检查 LLM_MODE 与 API Key）")

        self.stdout.write("\n发起一次真实调用（会消耗少量 token）…")
        try:
            result = client.complete_json(
                node="n1",
                system='你是接口自检器。只输出 JSON：{"ok": true, "echo": "收到的内容"}',
                user="ping",
                model=llm.model_extract,
                cache_material=f"llm-check:{__import__('time').time()}",
            )
        except QuotaExceeded as exc:
            raise CommandError(f"配额已用尽：{exc}") from exc
        except LlmError as exc:
            message = str(exc)
            self.stdout.write(self.style.ERROR(f"\n调用失败：{message}"))
            if "401" in message or "403" in message:
                self.stdout.write(f"  → {HINTS[401]}")
            elif "404" in message:
                self.stdout.write(f"  → {HINTS[404]}")
            elif "429" in message:
                self.stdout.write(f"  → {HINTS[429]}")
            raise SystemExit(1) from exc

        self.stdout.write(self.style.SUCCESS("\n调用成功"))
        self.stdout.write(f"  模型      {result.model}")
        self.stdout.write("  耗时      —（token 见下）")
        self.stdout.write(f"  tokens    in={result.tokens_in} out={result.tokens_out}")
        self.stdout.write(f"  成本      ¥{result.cost}")
        self.stdout.write(f"  返回数据  {result.data}")
        self.stdout.write(
            "\n下一步：把 LLM_MODE 设为 openai（已设）、确认模型名后重启 web/worker，"
            "分析链路会自动走 LLM，失败仍会降级到规则模式。"
        )
