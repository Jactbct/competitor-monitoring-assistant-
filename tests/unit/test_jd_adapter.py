"""京东站点适配器 + 促销去重（两个都是踩出来的）。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from domain.extract.rules import detect_site, extract

JD_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "jd"
JD_URL = "https://item.jd.com/10012345678.html"


def _extract(day: str):
    html = (JD_DIR / day / "10012345678.html").read_text(encoding="utf-8")
    return extract(html, JD_URL)


def test_site_detection():
    assert detect_site(JD_URL).name == "jd"
    assert detect_site("https://item.jd.hk/1.html").name == "jd"
    assert detect_site("https://item.taobao.com/item.htm?id=1").name == "taobao"
    assert detect_site("https://example.com/p/1").name == "taobao"  # 未知站点退回淘宝档


def test_jd_markers_and_basic_fields():
    ex = _extract("day0")

    assert ex.site == "jd"
    assert ex.strong_hits == ex.markers_total == 6
    assert "影驰" in ex.fields["title"]
    assert ex.fields["shop_name"] == "影驰京东自营旗舰店"
    assert ex.fields["sku_count"] == 3
    assert "品牌" in ex.fields["attrs"]


@pytest.mark.parametrize(
    ("day", "display", "final"),
    [
        ("day0", "2499.00", "2399.00"),  # 平台券 100
        ("day1", "2499.00", "2319.00"),  # 平台券 + 店铺券 80
        ("day2", "2399.00", "2219.00"),  # 调价 + 两张券
    ],
)
def test_final_price_per_day(day, display, final):
    ex = _extract(day)

    assert str(ex.fields["price_display"]) == display
    assert str(ex.price.final) == final
    assert ex.price.confidence == "certain"


def test_promos_are_not_double_counted():
    """回归：整页被压成一句话时，促销区里的券会被重复解析（曾把 2219 算成 2119）。"""
    ex = _extract("day2")

    applied = [line for line in ex.price.lines if line.applied]
    assert len(applied) == 2, f"应只有平台券和店铺券两条，实际 {[line.label for line in applied]}"
    assert sum(line.amount for line in applied) == Decimal("180.00")
    # 任何一条促销都不应出现两次
    labels = [line.label for line in ex.price.lines]
    assert len(labels) == len(set(labels))
