"""评测门禁（补充文档 §6.6）。

不通过就不许合：
* 到手价与人工标注一致率 = 100%
* 证据子串命中率 = 100%
* 幻觉计数 = 0（输出里的每个金额都必须能在原文找到）
* 强结构标记命中 ≥ 2

构造集来自 ``tests/fixtures/pages``。真实抽样集需要在 M1 从线上采集里人工标注，
本文件把它做成「同一套断言 + 不同数据源」，接入 CI 只需要换目录。
"""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from pathlib import Path

import pytest

from domain.extract.rules import extract
from domain.extract.text import normalize

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "pages"

#: 人工标注：文件 → (到手价, 是否确定)
LABELS = {
    "1001.html": (Decimal("114.00"), True),
    "1002.html": (Decimal("79.00"), True),
    "1003.html": (Decimal("229.00"), True),
}


def _page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("filename", sorted(LABELS))
def test_price_matches_label(filename):
    expected, certain = LABELS[filename]
    result = extract(_page(filename), f"https://item.taobao.com/item.htm?id={filename[:4]}")

    assert result.price is not None
    assert result.price.final == expected, f"{filename} 到手价 {result.price.final} ≠ 标注 {expected}"
    assert (result.price.confidence == "certain") is certain
    assert result.strong_hits >= 2


@pytest.mark.parametrize("filename", sorted(LABELS))
def test_evidence_is_verbatim_substring(filename):
    html = _page(filename)
    result = extract(html, "https://item.taobao.com/item.htm?id=1")
    page_text = " ".join(result.promo_texts)

    assert result.promo_items, f"{filename} 未解析出促销"
    for promo in result.promo_items:
        if not promo.evidence_text:
            continue
        assert normalize(promo.evidence_text) in page_text, f"证据不是原文子串：{promo.evidence_text}"


@pytest.mark.parametrize("filename", sorted(LABELS))
def test_no_hallucinated_amounts(filename):
    """输出里的每个金额都必须能在原文里找到 —— 幻觉计数必须为 0。"""
    html = _page(filename)
    page_text = normalize(html)
    result = extract(html, "https://item.taobao.com/item.htm?id=1")

    for promo in result.promo_items:
        for amount in (promo.threshold_yuan, promo.discount_yuan):
            if amount is None:
                continue
            assert normalize(str(amount.normalize())) in page_text or str(amount) in page_text


def test_abnormal_fixtures_are_recognised():
    import os

    os.environ["APP_ENV"] = "dev"
    from domain.collect.abnormal import sniff_abnormal
    from domain.extract.rules import extract as run_extract

    login_html = (FIXTURES / "login_wall.html").read_text(encoding="utf-8")
    result = run_extract(login_html, "https://item.taobao.com/item.htm?id=9")
    verdict = sniff_abnormal(
        http_status=200, body_bytes=len(login_html.encode()), text=login_html, strong_hits=result.strong_hits
    )
    assert verdict.abnormal_type == "LOGIN_WALL"

    broken = (FIXTURES / "structure_changed.html").read_text(encoding="utf-8")
    result = run_extract(broken, "https://item.taobao.com/item.htm?id=9")
    verdict = sniff_abnormal(
        http_status=200, body_bytes=len(broken.encode()), text=broken, strong_hits=result.strong_hits
    )
    assert verdict.abnormal_type == "STRUCTURE_CHANGED"


def test_degraded_rate_budget():
    """降级率 ≤ 15%（超过不阻塞发布，但必须说明原因）。"""
    from datetime import datetime

    from domain.analyze.decide import AnalysisContext, decide

    results = []
    for filename in LABELS:
        result = extract(_page(filename), "https://item.taobao.com/item.htm?id=1")
        ctx = AnalysisContext(
            now=datetime(2026, 10, 21, 10, 0, tzinfo=UTC),
            changed_fields=["price_final"],
            event_types=["real_price"],
            deltas={"price_final": {"before": "199.00", "after": str(result.price.final)}},
            magnitude=Decimal("0.2"),
            cur_fields=result.fields,
            cur_price_final=result.price.final,
            cur_price_display=result.price.display,
            price_uncertain=result.price.is_uncertain,
            our_title="同类商品",
            our_price=Decimal("199.00"),
            our_cost=Decimal("110.00"),
            evidence=[{"snapshot_evidence_id": "snap-1", "quote": "价格", "captured_at": "2026-10-21T10:00:00"}],
        )
        results.append(decide(ctx))

    degraded = sum(1 for d in results if d.degraded)
    assert degraded / len(results) <= 0.15
    assert all(d.card.get("conclusion") for d in results)
