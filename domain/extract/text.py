"""文本归一化工具。所有证据子串校验都建立在同一套归一化上。"""

from __future__ import annotations

import html as html_lib
import re
import unicodedata
from decimal import Decimal, InvalidOperation

_TAG_RE = re.compile(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", re.S | re.I)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[\s\u00a0\u3000]+")
_CURRENCY_RE = re.compile(r"[¥￥元,]")

__all__ = ["money", "normalize", "text_of", "title_gap_terms", "title_tokens"]


def normalize(text: str) -> str:
    """全半角统一 + 空白折叠。证据比对必须两边都过这个函数。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return _WS_RE.sub(" ", text).strip()


def text_of(fragment: str) -> str:
    """HTML 片段 → 可读文本（去脚本/样式/标签/实体）。"""
    if not fragment:
        return ""
    fragment = _TAG_RE.sub(" ", fragment)
    fragment = _ANY_TAG_RE.sub(" ", fragment)
    return normalize(html_lib.unescape(fragment))


def money(raw: str | None) -> Decimal | None:
    """'¥1,299.00' → Decimal('1299.00')；无法解析返回 None（绝不猜）。"""
    if raw is None:
        return None
    cleaned = _CURRENCY_RE.sub("", normalize(raw))
    if not cleaned or not re.fullmatch(r"\d+(?:\.\d{1,2})?", cleaned):
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if value <= 0 or value > Decimal("1000000"):
        return None
    return value.quantize(Decimal("0.01"))


#: 标题切词用的停用词（中文标题里高频但无信息量的片段）
_STOP_BIGRAMS = {
    "官方", "旗舰", "正品", "包邮", "新款", "全新", "品质", "优选", "专柜", "同款",
    "现货", "特价", "热卖", "爆款", "支持", "店铺", "商品", "宝贝", "活动", "优惠",
}


def title_tokens(title: str) -> set[str]:
    """无第三方分词依赖的标题词集：英文/数字按词，中文按 2-gram。"""
    text = normalize(title)
    if not text:
        return set()
    latin = set(re.findall(r"[A-Za-z][A-Za-z0-9\-+.]{1,}", text))
    han = re.sub(r"[^\u4e00-\u9fff]", " ", text)
    bigrams: set[str] = set()
    for chunk in han.split():
        if len(chunk) == 1:
            continue
        bigrams.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return {t.lower() for t in (latin | bigrams)} - _STOP_BIGRAMS


_HAN_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-+.]{1,}")


def title_gap_terms(our_title: str, comp_title: str, *, max_terms: int = 6) -> list[str]:
    """标题词差集 —— 只从**商品词表**里取，输出可直接补进标题的词。

    为什么不用 n-gram 差集：那会把「五分袖打底衫」切成「分袖」「袖打底」这类碎片，
    运营拿到无法执行，等于把噪音当建议（这是本项目最容易犯的「看起来智能」错误）。
    词表未覆盖的新品类，会退化为「只给属性补全建议」，这是可接受的降级。
    """
    from domain.extract.lexicon import LEXICON

    our = normalize(our_title)
    comp = normalize(comp_title)
    if not our or not comp:
        return []

    hits = [(comp.find(term), term) for term in LEXICON if term in comp and term not in our]
    hits.sort(key=lambda item: (item[0], -len(item[1])))

    selected: list[str] = []
    for _, term in hits:
        if any(term in existing for existing in selected):
            continue
        selected.append(term)
        if len(selected) >= max_terms:
            break
    return selected
