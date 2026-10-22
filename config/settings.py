"""Django 配置。

配置值的**唯一来源**是环境变量，集中校验在 :mod:`core.config`（启动即 fail-fast）。
"""

from __future__ import annotations

from pathlib import Path

from core.config import get_settings
from core.log import setup_logging

BASE_DIR = Path(__file__).resolve().parent.parent
APP = get_settings()

SECRET_KEY = APP.secret_key
DEBUG = APP.debug
ALLOWED_HOSTS = list(APP.allowed_hosts)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "shared.apps.SharedConfig",
    "tenancy.apps.TenancyConfig",
    "jobs.apps.JobsConfig",
    "web.apps.WebConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "web.middleware.RequestContextMiddleware",
    "web.middleware.TenantContextMiddleware",
    "web.middleware.ErrorHandlingMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "web" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "web.context.nav_context",
            ],
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"

if APP.is_postgres:
    _db = APP.db.parsed()
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _db["NAME"],
            "USER": _db["USER"],
            "PASSWORD": _db["PASSWORD"],
            "HOST": _db["HOST"],
            "PORT": _db["PORT"],
            "CONN_MAX_AGE": 60,
            "ATOMIC_REQUESTS": False,
            # 两个 schema 的表名全局唯一，靠 search_path 解析；
            # 表本身由 shared/migrations/0002 搬进 shared / app。
            "OPTIONS": {
                "application_name": "competitor-agent",
                "options": "-c search_path=app,shared,public",
            },
        }
    }
else:
    (BASE_DIR / ".data").mkdir(exist_ok=True)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / ".data" / "db.sqlite3",
            "OPTIONS": {"timeout": 20},
        }
    }

AUTH_USER_MODEL = "tenancy.AppUser"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
]

# 会话认证：httpOnly + SameSite=Lax，30 分钟滑动过期（架构文档 §8.3）
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 30 * 60
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_SECURE = APP.is_prod
CSRF_COOKIE_SECURE = APP.is_prod
CSRF_COOKIE_HTTPONLY = False
CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS if h not in {"127.0.0.1", "localhost"}]
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
if APP.is_prod:
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31_536_000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = APP.digest_timezone
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / ".data" / "static"
STATICFILES_DIRS = [BASE_DIR / "web" / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = __import__("os").environ.get("SMTP_HOST", "")
EMAIL_PORT = int(__import__("os").environ.get("SMTP_PORT", "465"))
EMAIL_HOST_USER = __import__("os").environ.get("SMTP_USER", "")
EMAIL_HOST_PASSWORD = __import__("os").environ.get("SMTP_PASSWORD", "")
# 没有超时的 SMTP 会永久占住一个 worker 线程（邮件服务器挂住时尤其致命）
EMAIL_TIMEOUT = 15
EMAIL_USE_SSL = EMAIL_PORT == 465
EMAIL_USE_TLS = EMAIL_PORT == 587
DEFAULT_FROM_EMAIL = APP.notify.mail_from or "alert@localhost"

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# 业务开关（都应经 core.config 读取，此处仅暴露给模板）
APP_NAME = "竞品监控"
LOGIN_URL = "/login"
LOGIN_REDIRECT_URL = "/"

setup_logging()
