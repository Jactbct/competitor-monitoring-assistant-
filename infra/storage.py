"""原始页存储（层 L-c）：gzip 落本地盘，只留 7 天，仅用于排障，不备份。

数据库里只放结构化字段与证据片段；原始 HTML 一旦进库，PG 的 autovacuum 会被拖死
（补充文档 C-3：不分层会从 240 GB 涨到 1.6 TB）。
"""

from __future__ import annotations

import contextlib
import gzip
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.utils import timezone

from core.config import get_settings

__all__ = ["StoredObject", "purge_expired", "read_raw", "save_raw", "store_stats"]


@dataclass
class StoredObject:
    key: str
    raw_bytes: int
    stored_bytes: int


def _root() -> Path:
    root = get_settings().raw_store_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_raw(target_id: int, content_sha256: str, body: bytes, captured_at: datetime) -> StoredObject:
    root = _root()
    relative = Path(f"{captured_at:%Y/%m}/{target_id}/{content_sha256[:32]}.html.gz")
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with gzip.open(path, "wb", compresslevel=6) as fh:
            fh.write(body)
    return StoredObject(key=str(relative).replace("\\", "/"), raw_bytes=len(body), stored_bytes=path.stat().st_size)


def read_raw(key: str) -> bytes | None:
    """按 key 读原始页。

    key 来自数据库（``snapshot.raw_object_key``），**必须**限定在根目录内：
    否则 ``../../etc/passwd`` 或绝对路径就是任意文件读取。
    """
    root = _root().resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    with gzip.open(path, "rb") as fh:
        return fh.read()


def purge_expired(retention_days: int | None = None) -> int:
    """删掉超过保留期的原始页，返回删除文件数。"""
    days = retention_days or get_settings().raw_retention_days
    cutoff = time.time() - days * 86400
    removed = 0
    for path in _root().rglob("*.html.gz"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    # 顺手清空目录
    for path in sorted(_root().rglob("*"), reverse=True):
        if path.is_dir():
            with contextlib.suppress(OSError):
                path.rmdir()
    return removed


def store_stats() -> dict:
    root = _root()
    files = list(root.rglob("*.html.gz"))
    size = sum(p.stat().st_size for p in files if p.is_file())
    oldest = min((p.stat().st_mtime for p in files), default=None)
    return {
        "files": len(files),
        "bytes": size,
        "oldest": (
            timezone.localtime(datetime.fromtimestamp(oldest, tz=UTC)).isoformat()
            if oldest
            else None
        ),
        "retention_days": get_settings().raw_retention_days,
        "expired_after": (datetime.now() - timedelta(days=get_settings().raw_retention_days)).isoformat(),
    }
