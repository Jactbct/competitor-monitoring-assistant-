"""采集预算：全局令牌桶（PG 单行原子更新，等价于 Redis 方案但少一个组件）。

规则（架构文档 §5.3 采集链路 ①）：**按可用额度分批放行**，拿不到就整轮退让。
不投递比「先投递再等预算」好得多 —— 后者会让任务堆在队列里，实际发出时已无预算，
出现「先狂发再熔断」的尖峰，那是最容易被风控抓到的形态。

桶容量默认 = 5 秒预算（吸收抖动，但不放大爆发）；出口速率恒等于 ``COLLECTOR_QPS``。
"""

from __future__ import annotations

from datetime import UTC

from django.db import connection, transaction

from core.config import get_settings
from core.db import is_postgres

__all__ = ["acquire_up_to", "capacity", "refill_now", "stats", "try_acquire"]

# GREATEST/MAX 兜住「时钟抖动导致 elapsed 为负」：桶只会被补，不会被时钟吃掉
_ARITH_PG = (
    "LEAST({cap}, GREATEST(tokens, tokens + EXTRACT(EPOCH FROM (now() - updated_at)) * {rate}))"
)
_ARITH_SQLITE = (
    "MIN({cap}, MAX(tokens, tokens + (julianday('now') - julianday(updated_at)) * 86400.0 * {rate}))"
)
_NOW_PG = "now()"
# SQLite 的 CURRENT_TIMESTAMP 只有秒精度，会让 1 QPS 的桶一次补满 1 个额度（测试与演示都会抖动）
_NOW_PG = "now()"


def capacity() -> float:
    """桶容量 = 5 秒预算。小于一轮到期量时，调度会自动分批。"""
    return max(get_settings().collector.qps * 5, 1.0)


def _table() -> str:
    from shared.models import RateBucket

    return RateBucket._meta.db_table


def _now_clause() -> str:
    """PG 用库内 now()；SQLite 由 Python 传入毫秒精度的 UTC 字符串（CURRENT_TIMESTAMP 只有秒）。"""
    return _NOW_PG if is_postgres() else "%(now)s"


def _now_text() -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _params(extra: dict | None = None) -> dict:
    data = {"now": _now_text()}
    if extra:
        data.update(extra)
    return data


def _ensure_row() -> None:
    """首次使用时把桶补满，避免「新装系统第一个 tick 永远拿不到预算」。"""
    with connection.cursor() as cur:
        cur.execute(
            f"INSERT INTO {_table()} (id, tokens, updated_at) VALUES (1, %(cap)s, {_now_clause()}) "
            "ON CONFLICT (id) DO NOTHING",
            _params({"cap": capacity()}),
        )


def acquire_up_to(n: int) -> int:
    """原子地取走最多 n 个额度，返回实际取到的数量（0 表示整轮退让）。

    实现是「行锁 + 读-算-写」，而不是一条花哨的 UPDATE：SQLite 与 PG 的
    时间函数不同（``julianday`` vs ``EXTRACT``），把算法放在 Python 里两边一致，
    也更好测。PG 下 ``FOR UPDATE`` 保证多进程安全；本项目同一时刻只有一个 worker。
    """
    if n <= 0:
        return 0
    rate = float(get_settings().collector.qps)
    cap = capacity()
    arith = (_ARITH_PG if is_postgres() else _ARITH_SQLITE).format(cap=cap, rate=rate)
    lock = " FOR UPDATE" if is_postgres() else ""

    with transaction.atomic():
        _ensure_row()
        with connection.cursor() as cur:
            cur.execute(f"SELECT {arith} FROM {_table()} WHERE id = 1{lock}")
            row = cur.fetchone()
            if row is None:
                return 0
            # 夹在 [0, cap]：SQLite 的 julianday 有毫秒级舍入，可能算出一个 -0.0005 的负数
            available = max(0.0, min(float(row[0] or 0.0), cap))
            granted = int(min(available, n) + 1e-9)
            if granted <= 0:
                return 0
            cur.execute(
                f"UPDATE {_table()} SET tokens = %(tokens)s, updated_at = {_now_clause()} WHERE id = 1",
                _params({"tokens": available - granted}),
            )
    return granted


def try_acquire(n: int = 1) -> bool:
    """拿满 n 个才算成功；不足时一个也不取（用于必须整体通过的场景）。"""
    return acquire_up_to(n) >= n


def refill_now() -> None:
    """把桶补满（压测/发布前用）。"""
    _ensure_row()
    with connection.cursor() as cur:
        cur.execute(
            f"UPDATE {_table()} SET tokens = %(cap)s, updated_at = {_now_clause()} WHERE id = 1",
            _params({"cap": capacity()}),
        )


def stats() -> dict:
    with connection.cursor() as cur:
        cur.execute(f"SELECT tokens, updated_at FROM {_table()} WHERE id = 1")
        row = cur.fetchone()
    return {
        "tokens": round(row[0], 3) if row else 0.0,
        "capacity": capacity(),
        "qps": get_settings().collector.qps,
        "updated_at": str(row[1]) if row and row[1] else None,
    }
