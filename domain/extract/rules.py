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
from urllib.parse import urlparse

from domain.extract.price import PriceResult, compute_final_price
from domain.extract.promo import PromoItem, parse_promos, promo_signature
from domain.extract.text import money, normalize, text_of

__all__ = ["EXTRACT_VERSION", "ExtractResult", "JD_MARKERS", "PROFILES", "STRONG_MARKERS", "detect_site", "extract"]

EXTRACT_VERSION = "rules-1.1"

#: 6 个必然出现在真实商品页的元素（架构文档 §10.2「强结构标记」）——淘宝
STRONG_MARKERS: dict[str, re.Pattern[str]] = {
    "price_num": re.compile(r"tb-rmb-num"),
    "title": re.compile(r"tb-main-title"),
    "main_image": re.compile(r"J_ImgBooth"),
    "sku": re.compile(r"(?:J_Sku|tb-sku)"),
    "promo_price": re.compile(r"(?:J_PromoPrice|tm-promo-price)"),
    "shop": re.compile(r"shop-name"),
}

#: 京东的强结构标记：与淘宝完全不同（这正是需要「站点适配器」的理由）
JD_MARKERS: dict[str, re.Pattern[str]] = {
    "price": re.compile(r'class="[^"]*\bp-price\b', re.I),
    "title": re.compile(r'class="[^"]*\bsku-name\b', re.I),
    "main_image": re.compile(r'id="spec-img"', re.I),
    "sku": re.compile(r'id="choose-attrs"|class="[^"]*\bsku-attr\b', re.I),
    "promo": re.compile(r'class="[^"]*(?:J-prom|promo-item|summary-promotion)', re.I),
    "shop": re.compile(r'class="[^"]*\bJ-hove-wrap\b|id="crumb-wrap"', re.I),
}


@dataclass(frozen=True)
class SiteProfile:
    """一个站点的解析适配器。

    为什么要有这层：淘宝与京东的商品页标记、价格所在标签、促销区容器没有一个相同。
    把站点差异集中成一个值对象后，`extract()` 只依赖接口；新增站点 = 加一个 profile，
    而不是在几十个 if 里改正则。
    """

    name: str
    markers: dict[str, re.Pattern[str]]
    title: tuple[re.Pattern[str], ...]
    price: tuple[re.Pattern[str], ...]
    promo_price: tuple[re.Pattern[str], ...]
    image: tuple[re.Pattern[str], ...]
    shop: tuple[re.Pattern[str], ...]
    promo_block: re.Pattern[str]
    sku_id: re.Pattern[str]
    sku_item: re.Pattern[str]
    attrs: re.Pattern[str]


_JD_TITLE = (
    re.compile(r'<div[^>]*class="[^"]*\bsku-name\b[^"]*"[^>]*>(.*?)</div>', re.S | re.I),
    re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I),
    re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I),
)
_JD_PRICE = (
    re.compile(
        r'<span[^>]*class="[^"]*\bprice\b[^"]*"[^>]*>\s*(?:￥|¥)?\s*([0-9]+(?:\.[0-9]{1,2})?)',
        re.I,
    ),
    re.compile(r'<em[^>]*class="[^"]*\bp-price\b[^"]*"[^>]*>(.*?)</em>', re.S | re.I),
    re.compile(r'"price"\s*:\s*"?([0-9]+(?:\.[0-9]{1,2})?)"?', re.I),
)
_JD_SHOP = (
    re.compile(r'<div[^>]*class="[^"]*\bshop-name\b[^"]*"[^>]*>(.*?)</div>', re.S | re.I),
    re.compile(r'<a[^>]*class="[^"]*\bname\b[^"]*"[^>]*>(.*?)</a>', re.S | re.I),
    re.compile(r'"shopName"\s*:\s*"([^"]{1,40})"', re.I),
)
_JD_IMAGE = (
    re.compile(r'<img[^>]*id="spec-img"[^>]*src="([^"]+)"', re.I),
    re.compile(r'<img[^>]*src="([^"]+)"[^>]*id="spec-img"', re.I),
)
_JD_PROMO_BLOCK = re.compile(
    r'<div[^>]*class="[^"]*(?:J-prom|promo-item|summary-promotion)[^"]*"[^>]*>(.*?)</div>',
    re.S | re.I,
)
_JD_SKU_ID = re.compile(r'data-sku="[^"]+"', re.I)
_JD_SKU_ITEM = re.compile(r'class="[^"]*\bsku-attr\b', re.I)

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

PROFILES: dict[str, SiteProfile] = {
    "taobao": SiteProfile(
        name="taobao",
        markers=STRONG_MARKERS,
        title=_TITLE_PATTERNS,
        price=_PRICE_NUM_PATTERNS,
        promo_price=_PROMO_PRICE_PATTERNS,
        image=_IMAGE_PATTERNS,
        shop=_SHOP_PATTERNS,
        promo_block=_PROMO_BLOCK_RE,
        sku_id=_SKU_ID_RE,
        sku_item=_SKU_ITEM_RE,
        attrs=_ATTR_RE,
    ),
    "jd": SiteProfile(
        name="jd",
        markers=JD_MARKERS,
        title=_JD_TITLE,
        price=_JD_PRICE,
        promo_price=_PROMO_PRICE_PATTERNS,
        image=_JD_IMAGE,
        shop=_JD_SHOP,
        promo_block=_JD_PROMO_BLOCK,
        sku_id=_JD_SKU_ID,
        sku_item=_JD_SKU_ITEM,
        attrs=_ATTR_RE,
    ),
}


def detect_site(url: str) -> SiteProfile:
    """按 URL 选解析适配器。

    未知站点退回淘宝档：这样未知页面会因为「强标记命中不足」被判 STRUCTURE_CHANGED，
    宁可明确报错，也不要静默抓一堆解析错的字段。
    """
    host = (urlparse(url).netloc or "").lower()
    if "jd.com" in host or "jd.hk" in host:
        return PROFILES["jd"]
    return PROFILES["taobao"]




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
    site: str = "taobao"
    markers_total: int = 6
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


def _promo_region_text(html: str, profile: SiteProfile | None = None) -> str:
    pattern = (profile or PROFILES["taobao"]).promo_block
    blocks = pattern.findall(html)
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
    profile = detect_site(url)
    markers = [name for name, pattern in profile.markers.items() if pattern.search(html)]
    completeness = (Decimal(len(markers)) / Decimal(len(profile.markers))).quantize(Decimal("0.001"))

    title = _first_match(profile.title, html)
    price_display = money(_first_match(profile.price, html))
    promo_price_raw = _first_match(profile.promo_price, html)
    promo_price = money(promo_price_raw)

    # 页面上同时给出展示价与促销价时，展示价仍是挂牌价；促销价参与规则解析
    if price_display is None and promo_price is not None:
        price_display = promo_price

    # SKU 数量优先按 data-sku-id 计（每行只有一个），没有该属性时退回 class 计数
    sku_ids = profile.sku_id.findall(html)
    sku_items = sku_ids if sku_ids else profile.sku_item.findall(html)
    attrs: dict[str, str] = {}
    for key, value in profile.attrs.findall(html):
        key = normalize(key)
        value = normalize(value)
        if key and value and len(attrs) < 30:
            attrs.setdefault(key, value)

    region = _promo_region_text(html, profile)
    page_text = text_of(html)[:200_000]
    promo_texts = _candidate_sentences(region or page_text)
    if region and page_text:
        # 促销区之外的页面级优惠（如平台券横幅）也纳入，但排在后面。
        # 两个必须做的过滤（都是真踩过的坑）：
        #  1. text_of() 会把整页压成**一句话**，这句里包含促销区文本 → 同一条券会被算两次
        #     （实测 2399-100-80 被算成 2119，多减了 100）。所以：包含已知促销句的整页句直接丢。
        #  2. 超长句子不是促销句，是页面转储，不参与解析。
        region_sentences = list(promo_texts)
        extra = [
            s
            for s in _candidate_sentences(page_text)
            if s not in region_sentences
            and len(s) <= 120
            and not any(r in s for r in region_sentences)
        ]
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
        "main_image": _first_match(profile.image, html),
        "shop_name": _first_match(profile.shop, html),
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
        site=profile.name,
        markers_total=len(profile.markers),
    )
