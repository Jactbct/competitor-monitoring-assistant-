"""采集探针：验证「这一页我们能不能解析」，**不落库、不改数据**。

两个用途，都是真实工作里最费时间的环节：

1. **接新类目**：拿 3~5 个链接跑一遍，看 6 个强结构标记命中几个、促销解析出什么、
   到手价算得对不对。命中 < 2 就说明 STRONG_MARKERS 要改，改完再跑。
2. **合规评审**：证明我们只取公开页的哪几个字段、不取评价与用户信息、单次请求、带 UA。

默认**只读样本页**（不产生任何外部请求）；要真抓必须显式加 ``--url``，
且仍然走同一套护栏（https / 域名白名单 / robots / 令牌桶 / 超时）。

用法：
    python manage.py probe_page --fixture tests/fixtures/pages/1001.html
    python manage.py probe_page --url "https://item.taobao.com/item.htm?id=1001" --save
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.config import get_settings
from domain.collect.abnormal import sniff_abnormal
from domain.collect.client import fetch_page
from domain.collect.guardrails import GuardrailError, normalize_url, validate_url
from domain.extract.rules import STRONG_MARKERS, extract
from infra import budget

STRUCTURE_MIN = 2


class Command(BaseCommand):
    help = "解析探针：打印强结构标记命中、字段、促销明细与到手价求解过程（不落库）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--url", help="真实抓取一次（走合规护栏，消耗 1 个令牌）")
        parser.add_argument("--fixture", help="读取本地样本页（不产生外部请求）")
        parser.add_argument("--save", action="store_true", help="把本次 HTML 存进 tests/fixtures/pages 作为回归样本")
        parser.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本消费")

    def handle(self, *args, **options) -> None:
        if not options["url"] and not options["fixture"]:
            raise CommandError("必须给 --url 或 --fixture 之一")

        settings = get_settings()
        url = options["url"] or ""
        html = ""
        meta: dict = {}

        if options["fixture"]:
            path = Path(options["fixture"])
            if not path.is_file():
                raise CommandError(f"样本页不存在：{path}")
            html = path.read_text(encoding="utf-8")
            url = url or "https://item.taobao.com/item.htm?id=probe"
            meta = {"source": "fixture", "path": str(path), "bytes": len(html.encode())}
        else:
            try:
                normalized = normalize_url(url)
                validate_url(normalized)
            except GuardrailError as exc:
                raise CommandError(f"护栏拒绝：{exc}") from exc
            if budget.acquire_up_to(1) < 1:
                raise CommandError("采集预算不足（令牌桶空），稍后重试；探针同样计入出口预算")
            result = fetch_page(normalized)
            html = result.text
            url = normalized
            meta = {
                "source": "http",
                "status": result.status_code,
                "bytes": len(result.body),
                "elapsed_ms": result.elapsed_ms,
                "etag": result.etag,
                "not_modified": result.not_modified,
            }

        if options["save"] and html:
            target_dir = settings.collector.fixture_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            stem = "probe"
            for part in url.split("?")[-1].split("&"):
                key, _, value = part.partition("=")
                if key in {"id", "itemId", "item_id"} and value.isdigit():
                    stem = value
                    break
            out = target_dir / f"{stem}.html"
            if options["fixture"]:
                shutil.copyfile(options["fixture"], out)
            else:
                out.write_text(html, encoding="utf-8")
            meta["saved_to"] = str(out)

        ex = extract(html, url)
        verdict = sniff_abnormal(
            http_status=meta.get("status", 200),
            body_bytes=meta.get("bytes", 0),
            text=html,
            strong_hits=ex.strong_hits,
        )

        payload = {
            "url": url,
            "fetch": meta,
            "structure": {
                "hits": ex.strong_hits,
                "total": len(STRONG_MARKERS),
                "markers": ex.strong_markers,
                "missing": [name for name in STRONG_MARKERS if name not in ex.strong_markers],
                "completeness": str(ex.completeness),
                "ok": ex.strong_hits >= STRUCTURE_MIN,
            },
            "abnormal": {"type": verdict.abnormal_type, "reason": verdict.reason},
            "fields": {k: v for k, v in ex.fields.items() if k not in {"evidence_blocks", "attrs"}},
            "attrs": ex.fields.get("attrs", {}),
            "promos": [item.to_dict() for item in ex.promo_items],
            "promo_conflicts": ex.promo_conflicts,
            "price": ex.price.to_dict() if ex.price else None,
        }

        if options["json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        self._render(payload)

        if not payload["structure"]["ok"]:
            self.stdout.write(
                self.style.ERROR(
                    f"\n强结构标记只命中 {ex.strong_hits} 个（<{STRUCTURE_MIN}）：这页现在解析不了，"
                    "需要改 STRONG_MARKERS 或正则后再跑"
                )
            )
            raise SystemExit(1)

    def _render(self, payload: dict) -> None:
        fetch = payload["fetch"]
        self.stdout.write(f"\nURL：{payload['url']}")
        if fetch.get("source") == "fixture":
            self.stdout.write(f"来源：样本页 {fetch['path']}（{fetch['bytes']} B）")
        elif fetch.get("not_modified"):
            self.stdout.write("来源：线上（304 未修改 —— 页面没变，不应产生快照）")
        else:
            self.stdout.write(
                f"来源：线上 HTTP {fetch.get('status')} · {fetch.get('bytes')} B · {fetch.get('elapsed_ms')} ms"
            )
        if fetch.get("saved_to"):
            self.stdout.write(f"已存为回归样本：{fetch['saved_to']}")

        structure = payload["structure"]
        mark = "OK" if structure["ok"] else "FAIL"
        self.stdout.write(
            f"\n强结构标记：{structure['hits']}/{structure['total']} [{mark}]  命中={structure['markers']}  缺={structure['missing']}"
        )
        if payload["abnormal"]["type"]:
            self.stdout.write(f"异常判定：{payload['abnormal']['type']} —— {payload['abnormal']['reason']}")

        fields = payload["fields"]
        self.stdout.write("\n字段：")
        for key in ("title", "price_display", "shop_name", "sku_count", "main_image", "item_id"):
            value = fields.get(key)
            if isinstance(value, str) and len(value) > 60:
                value = value[:60] + "…"
            self.stdout.write(f"  {key:14} {value}")
        self.stdout.write(f"  {'attrs':14} {len(payload['attrs'])} 项 {list(payload['attrs'])[:6]}")

        self.stdout.write("\n促销解析：")
        if not payload["promos"]:
            self.stdout.write("  （无）")
        for item in payload["promos"]:
            self.stdout.write(
                f"  {item['activity_type']:6} 门槛={item['threshold_yuan']} 面额={item['discount_yuan']}"
                f"  证据={item['evidence_text'][:40]}"
            )
        if payload["promo_conflicts"]:
            self.stdout.write(self.style.WARNING(f"  冲突：{payload['promo_conflicts']}"))

        price = payload["price"] or {}
        self.stdout.write(
            f"\n到手价：展示 {price.get('display')} → 到手 {price.get('final')}"
            f"  置信度={price.get('confidence')}"
        )
        for line in price.get("lines", []):
            flag = "生效" if line["applied"] else "未生效"
            self.stdout.write(f"  [{flag}] {line['label']}  减 {line['amount']}  {line['note']}")
        if price.get("reasons"):
            self.stdout.write(f"  不确定原因：{price['reasons']}")
        self.stdout.write("")
