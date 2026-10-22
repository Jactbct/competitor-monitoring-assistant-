"""集中式配置：环境变量 → 强类型 Settings，启动时 fail-fast。

设计约束（对应架构文档 §5.2「core/config.py 环境变量集中校验」）：

* 本项目**不做**运行时热改配置。采集频率（COLLECTOR_QPS）尤其只能改文件后重启，
  这是合规要求（§8.3：「不提供后台旋钮」）。
* 缺失必需项时抛 :class:`ConfigError`，不带着半套配置启动。
* 本模块只依赖标准库，`domain/*` 可以安全 import。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

__all__ = [
    "ConfigError",
    "CollectorSettings",
    "DatabaseSettings",
    "LlmSettings",
    "NotifySettings",
    "Settings",
    "get_settings",
    "reset_settings_cache",
]


class ConfigError(RuntimeError):
    """配置错误。启动阶段抛出，进程直接退出。"""


# --------------------------------------------------------------------------- #
# 读取原语
# --------------------------------------------------------------------------- #
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _load_env_file(path: Path) -> None:
    """极简 .env 读取（不覆盖已存在的环境变量）。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _str(name: str, default: str = "", required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"缺少必需环境变量：{name}")
    return value


def _int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数，实际为 {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"环境变量 {name} 不能小于 {minimum}，实际为 {value}")
    return value


def _float(name: str, default: float, *, positive: bool = False) -> float:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是数字，实际为 {raw!r}") from exc
    if positive and value <= 0:
        raise ConfigError(f"环境变量 {name} 必须为正数，实际为 {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"环境变量 {name} 必须是布尔值（1/0/true/false），实际为 {raw!r}")


def _choice(name: str, default: str, allowed: set[str]) -> str:
    value = os.environ.get(name, default).strip()
    if value not in allowed:
        raise ConfigError(f"环境变量 {name} 只能是 {sorted(allowed)}，实际为 {value!r}")
    return value


# --------------------------------------------------------------------------- #
# 配置分组
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DatabaseSettings:
    """数据库连接。url 为空时落到 SQLite（仅开发/演示）。"""

    url: str = ""

    @property
    def is_postgres(self) -> bool:
        return self.url.startswith(("postgres://", "postgresql://"))

    @property
    def engine(self) -> str:
        return "postgresql" if self.is_postgres else "sqlite"

    def parsed(self) -> dict[str, object]:
        if not self.is_postgres:
            return {}
        parts = urlparse(self.url)
        return {
            "NAME": (parts.path or "/competitor").lstrip("/"),
            "USER": parts.username or "",
            "PASSWORD": parts.password or "",
            "HOST": parts.hostname or "127.0.0.1",
            "PORT": str(parts.port or 5432),
        }


@dataclass(frozen=True)
class CollectorSettings:
    """采集侧配置。qps 是出口硬上限，只能改配置 + 重启。"""

    qps: float = 1.0
    mode: str = "fixture"  # fixture | http
    user_agent: str = "TaobaoCompetitorMonitor/0.1 (+mailto:ops@example.com)"
    timeout_s: float = 12.0
    max_bytes: int = 3_000_000
    robots_enforce: bool = True
    fixture_dir: Path = field(default_factory=lambda: Path("tests/fixtures/pages"))
    circuit_fail_threshold: int = 5
    circuit_cooldown_min: int = 30
    circuit_max_cooldown_min: int = 1440
    active_hours: tuple[int, int] = (8, 23)
    idle_interval_factor: int = 3

    def validate(self) -> None:
        if self.qps <= 0 or self.qps > 5:
            raise ConfigError(f"COLLECTOR_QPS 必须在 (0, 5] 内（合规预算），实际为 {self.qps}")
        ua = self.user_agent
        if "mailto:" not in ua and "@" not in ua:
            raise ConfigError("COLLECTOR_UA 必须包含可联系的邮箱（合规要求，见 §8.3）")
        if "Monitor" not in ua and "Bot" not in ua:
            raise ConfigError("COLLECTOR_UA 必须包含可识别的产品标识（合规要求，见 §8.3）")


@dataclass(frozen=True)
class LlmSettings:
    mode: str = "off"  # off | openai
    base_url: str = ""
    api_key: str = ""
    model_extract: str = "small"
    model_reason: str = "mid"
    prompt_version: str = "p1"
    timeout_s: float = 30.0
    max_input_chars: int = 6000
    daily_token_quota: int = 2_000_000
    cache_ttl_days: int = 30

    @staticmethod
    def normalize_base_url(raw: str) -> str:
        """把用户粘贴的地址统一成「base」形式。

        供应商文档常给完整 endpoint（``https://host/v1/chat/completions``），
        而客户端会自己拼 ``/chat/completions``。不归一化就会请求成
        ``.../chat/completions/chat/completions`` —— 401/404 之后很难看出原因。
        """
        url = (raw or "").strip().rstrip("/")
        for suffix in ("/chat/completions", "/completions", "/v1/chat/completions"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        return url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return self.mode == "openai" and bool(self.api_key)

    def validate(self) -> None:
        if self.mode not in {"off", "openai"}:
            raise ConfigError(f"LLM_MODE 只能是 off/openai，实际为 {self.mode!r}")
        # 这里刻意**不**抛错：模型是软依赖。缺 Key 时整条链路会确定性降级到规则引擎
        # （卡片照样生成，只是归因粒度粗）。如果在这里 fail-fast，一个还没填 Key 的
        # .env 会让登录页都打不开，属于「配置没填好 → 全站不可用」的错误取舍。
        if self.mode == "openai" and not self.api_key:
            logging.getLogger("config").warning(
                "LLM_MODE=openai 但 LLM_API_KEY 为空：模型调用将降级为规则模式（运行 manage.py llm_check 查看详情）"
            )


@dataclass(frozen=True)
class NotifySettings:
    wecom_webhook: str = ""
    mail_from: str = ""
    digest_hour: int = 9

    @property
    def has_any_channel(self) -> bool:
        return bool(self.wecom_webhook or self.mail_from)


@dataclass(frozen=True)
class Settings:
    env: str
    secret_key: str
    debug: bool
    allowed_hosts: tuple[str, ...]
    db: DatabaseSettings
    collector: CollectorSettings
    llm: LlmSettings
    notify: NotifySettings
    raw_store_dir: Path
    raw_retention_days: int
    snapshot_retention_days: int
    job_poll_interval_s: float
    job_batch_size: int
    job_max_attempts: int
    digest_timezone: str
    trusted_proxy: bool

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def is_postgres(self) -> bool:
        return self.db.is_postgres


def _build(project_root: Path) -> Settings:
    env = _choice("APP_ENV", "dev", {"dev", "staging", "prod"})
    secret_key = _str("DJANGO_SECRET_KEY", "dev-only-change-me")
    if env == "prod" and secret_key in {"", "dev-only-change-me"}:
        raise ConfigError("生产环境必须设置独立的 DJANGO_SECRET_KEY")

    database_url = _str("DATABASE_URL", "")
    if env == "prod" and not database_url.startswith(("postgres://", "postgresql://")):
        # 生产必须 PG：RLS / JSONB / text[] / SKIP LOCKED 都是硬依赖（架构文档 §6.3）
        raise ConfigError("生产环境必须使用 PostgreSQL（DATABASE_URL 以 postgresql:// 开头）")

    allowed = tuple(h.strip() for h in _str("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip())

    collector = CollectorSettings(
        qps=_float("COLLECTOR_QPS", 1.0, positive=True),
        mode=_choice("COLLECTOR_MODE", "fixture", {"fixture", "http"}),
        user_agent=_str("COLLECTOR_UA", "TaobaoCompetitorMonitor/0.1 (+mailto:ops@example.com)"),
        timeout_s=_float("COLLECTOR_TIMEOUT_S", 12.0, positive=True),
        max_bytes=_int("COLLECTOR_MAX_BYTES", 3_000_000, minimum=1024),
        robots_enforce=_bool("ROBOTS_ENFORCE", True),
        fixture_dir=Path(_str("COLLECTOR_FIXTURE_DIR", str(project_root / "tests/fixtures/pages"))),
    )
    collector.validate()

    llm = LlmSettings(
        mode=_str("LLM_MODE", "off"),
        base_url=LlmSettings.normalize_base_url(_str("LLM_BASE_URL", "")),
        api_key=_str("LLM_API_KEY", ""),
        model_extract=_str("LLM_MODEL_EXTRACT", "small"),
        model_reason=_str("LLM_MODEL_REASON", "mid"),
        prompt_version=_str("LLM_PROMPT_VERSION", "p1"),
        timeout_s=_float("LLM_TIMEOUT_S", 30.0, positive=True),
        daily_token_quota=_int("LLM_DAILY_TOKEN_QUOTA", 2_000_000, minimum=0),
    )
    llm.validate()

    return Settings(
        env=env,
        secret_key=secret_key,
        debug=_bool("DJANGO_DEBUG", env != "prod"),
        allowed_hosts=allowed,
        db=DatabaseSettings(url=database_url),
        collector=collector,
        llm=llm,
        notify=NotifySettings(
            wecom_webhook=_str("WECOM_WEBHOOK", ""),
            mail_from=_str("MAIL_FROM", ""),
            digest_hour=_int("DIGEST_HOUR", 9, minimum=0),
        ),
        raw_store_dir=Path(_str("RAW_STORE_DIR", str(project_root / ".data/raw"))),
        raw_retention_days=_int("RAW_RETENTION_DAYS", 7, minimum=1),
        # 结构化快照保留期：400 天 ≈ 覆盖一年同比分析，再长就只是成本
        snapshot_retention_days=_int("SNAPSHOT_RETENTION_DAYS", 400, minimum=30),
        job_poll_interval_s=_float("JOB_POLL_INTERVAL_S", 1.0, positive=True),
        job_batch_size=_int("JOB_BATCH_SIZE", 20, minimum=1),
        job_max_attempts=_int("JOB_MAX_ATTEMPTS", 3, minimum=1),
        digest_timezone=_str("APP_TIMEZONE", "Asia/Shanghai"),
        # 部署在 Nginx/SLB 之后才设为 1，否则 X-Forwarded-For 可被客户端伪造
        trusted_proxy=_bool("TRUSTED_PROXY", False),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试里用 :func:`reset_settings_cache` 重置。"""
    root = Path(__file__).resolve().parent.parent
    _load_env_file(root / ".env")
    return _build(root)


def reset_settings_cache() -> None:
    get_settings.cache_clear()
