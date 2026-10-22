"""规则抽取器：HTML → 结构化快照字段（确定性，无网络、无 LLM）。

两个用途：
1. 采集后立即产出事实（不依赖 LLM，宕机也能出数据）；
2. 作为 LLM 抽取的**对照系**：数字冲突时以本模块为准（补充文档 §6.2）。

「强结构标记」判据来自真实流量验证过的原型，别改着玩：
命中数 < 2 说明页面是空壳/改版页，必须判异常，否则下游会静默失效（SPO-2）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal

from domain.extract.price import PriceResult, compute_final_price
from domain.extract.promo import PromoItem, parse_promos, promo_signature
from domain.extract.text import money, normalize, text_of

__all__ = ["EXTRACT_VERSION", "ExtractResult", "STRONG_MARKERS", "extract"]

EXTRACT_VERSION = "rules-1.0"

#: 6 个必然出现在真实商品页的元素（架构文档 §10.2「强结构标记」）
STRONG_MARKERS: dict[str, re.Pattern[str]] = {
    "price_num": re.compile(r"tb-rmb-num"),
    "title": re.compile(r"tb-main-title"),
    "main_image": re.compile(r"J_ImgBooth"),
    "sku": re.compile(r"(?:J_Sku|tb-sku)"),
    "promo_price": re.compile(r"(?:J_PromoPrice|tm-promo-price)"),
    "shop": re.compile(r"shop-name"),
}

_TITLE_PATTERNS = (
    re.compile(r"<h1[^>]*class=\"[^\"]*tb-main-title[^\"]*\"[^>]*>(.*?)</h1>", re.S | re.I),
    re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I),
    re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I),
)
_PRICE_NUM_PATTERNS = (
    re.compile(r"<em[^>]*class=\"[^\"]*tb-rmb-num[^\"]*\"[^>]*>(.*?)</em>", re.S | re.I),
    re.compile(r"\"price\"\s*:\s*\"?([0-9]+(?:\.[0-9]{1,2})?)\"?", re.I),
    re.compile(r"class=\"[^\"]*tb-rmb-num[^\"]*\"[^>]*>(.*?)<", re.S | re.I),
)
_PROMO_PRICE_PATTERNS = (
    re.compile(
        r"<(?:em|span|div)[^>]*class=\"[^\"]*(?:J_PromoPrice|tm-promo-price)[^\"]*\"[^>]*>(.*?)</", re.S | re.I
    ),
    re.compile(r"\"promotionPrice\"\s*:\s*\"?([0-9]+(?:\.[0-9]{1,2})?)\"?", re.I),
)
_IMAGE_PATTERNS = (
    re.compile(r"<img[^>]*id=\"J_ImgBooth\"[^>]*src=\"([^\"]+)\"", re.I),
    re.compile(r"<img[^>]*src=\"([^\"]+)\"[^>]*id=\"J_ImgBooth\"", re.I),
    re.compile(r"\"picUrl\"\s*:\s*\"(//[^\"]+)\"", re.I),
)
_SHOP_PATTERNS = (
    re.compile(r"class=\"[^\"]*shop-name[^\"]*\"[^>]*>(.*?)<", re.S | re.I),
    re.compile(r"\"shopName\"\s*:\s*\"([^\"]{1,40})\"", re.I),
)
_SKU_ITEM_RE = re.compile(r"data-sku-id=\"[^\"]+\"|class=\"[^\"]*tb-sku-item[^\"]*\"", re.I)
_SKU_ID_RE = re.compile(r"data-sku-id=\"[^\"]+\"", re.I)
_ATTR_RE = re.compile(r"<li[^>]*>\s*([^<>：:]{1,12})\s*[：:]\s*([^<>]{1,60})\s*</li>", re.S)
_PROMO_BLOCK_RE = re.compile(
    r"<div[^>]*class=\"[^\"]*(?:tb-promo|promo-info|J_Promo|tm-promo)[^\"]*\"[^>]*>(.*?)</div>", re.S | re.I
)
_ITEM_ID_RE = re.compile(r"[?&](?:id|itemId|item_id)=(\d{5,20})")
_SHOP_ID_RE = re.compile(r"[?&](?:shopId|shop_id|user_number_id)=(\d{3,20})")
_PROMO_KEYWORD_RE = re.compile(r"满\s*\d|每满|立减|秒杀|元券|店铺券|平台券|赠品|加赠|买\d+送")


@dataclass
class ExtractResult:
    fields: dict
    completeness: Decimal
    strong_hits: int
    strong_markers: list[str] = field(default_factory=list)
    promo_texts: list[str] = field(default_factory=list)
    promo_items: list[PromoItem] = field(default_factory=list)
    promo_conflicts: list[str] = field(default_factory=list)
    price: PriceResult | None = None
    extract_version: str = EXTRACT_VERSION

    @property
    def structure_ok(self) -> bool:
        return self.strong_hits >= 2

    @property
    def promo_signature(self) -> str:
        return promo_signature(self.promo_items)

    def to_snapshot_fields(self) -> dict:
        return {
            **self.fields,
            "promo_signature": self.promo_signature,
            "price_detail": self.price.to_dict() if self.price else None,
        }


def _first_match(patterns: tuple[re.Pattern[str], ...], html: str) -> str:
    for pattern in patterns:
        match = pattern.search(html)
        if match:
            return text_of(match.group(1))
    return ""


def _promo_region_text(html: str) -> str:
    blocks = _PROMO_BLOCK_RE.findall(html)
    if not blocks:
        return ""
    return " | ".join(text_of(b) for b in blocks if b.strip())


def _candidate_sentences(text: str, limit: int = 40) -> list[str]:
    """从页面文本里挑出含促销关键词的句子，作为规则解析与证据引文的来源。"""
    sentences = [s.strip() for s in re.split(r"[。；;|\n]", text) if s and s.strip()]
    hits = [s for s in sentences if _PROMO_KEYWORD_RE.search(s)]
    return hits[:limit]


def compute_content_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def extract(html: str, url: str = "") -> ExtractResult:
    """纯函数：不访问网络、不写库、不调模型。"""
    html = html or ""
    markers = [name for name, pattern in STRONG_MARKERS.items() if pattern.search(html)]
    completeness = (Decimal(len(markers)) / Decimal(len(STRONG_MARKERS))).quantize(Decimal("0.001"))

    title = _first_match(_TITLE_PATTERNS, html)
    price_display = money(_first_match(_PRICE_NUM_PATTERNS, html))
    promo_price_raw = _first_match(_PROMO_PRICE_PATTERNS, html)
    promo_price = money(promo_price_raw)

    # 页面上同时给出展示价与促销价时，展示价仍是挂牌价；促销价参与规则解析
    if price_display is None and promo_price is not None:
        price_display = promo_price

    # SKU 数量优先按 data-sku-id 计（每行只有一个），没有该属性时退回 class 计数
    sku_ids = _SKU_ID_RE.findall(html)
    sku_items = sku_ids if sku_ids else _SKU_ITEM_RE.findall(html)
    attrs: dict[str, str] = {}
    for key, value in _ATTR_RE.findall(html):
        key = normalize(key)
        value = normalize(value)
        if key and value and len(attrs) < 30:
            attrs.setdefault(key, value)

    region = _promo_region_text(html)
    page_text = text_of(html)[:200_000]
    promo_texts = _candidate_sentences(region or page_text)
    if region and page_text:
        # 促销区之外的页面级优惠（如平台券横幅）也纳入，但排在后面
        extra = [s for s in _candidate_sentences(page_text) if s not in promo_texts]
        promo_texts = (promo_texts + extra)[:40]

    parsed = parse_promos(promo_texts)
    promos = list(parsed.items)
    if promo_price is not None and price_display is not None and promo_price < price_display:
        promos.append(
            PromoItem(
                activity_type="立减",
                discount_yuan=(price_display - promo_price).quantize(Decimal("0.01")),
                evidence_text="",
                confidence=1.0,
                source="rules",
            )
        )

    price_result = compute_final_price(price_display, promos)

    fields = {
        "title": title,
        "price_display": str(price_display) if price_display is not None else None,
        "main_image": _first_match(_IMAGE_PATTERNS, html),
        "shop_name": _first_match(_SHOP_PATTERNS, html),
        "sku_count": len(sku_items),
        "attrs": attrs,
        "item_id": (m.group(1) if (m := _ITEM_ID_RE.search(url)) else ""),
        "shop_id": (m.group(1) if (m := _SHOP_ID_RE.search(url)) else ""),
        "strong_markers": markers,
        "evidence_blocks": promo_texts[:10],
    }

    return ExtractResult(
        fields=fields,
        completeness=completeness,
        strong_hits=len(markers),
        strong_markers=markers,
        promo_texts=promo_texts,
        promo_items=promos,
        promo_conflicts=parsed.conflicts,
        price=price_result,
    )
