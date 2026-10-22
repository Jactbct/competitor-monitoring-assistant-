# ---------- 运行 ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings

WORKDIR /app

# curl 供 compose healthcheck 使用；其余依赖全部走 pip（无系统级编译依赖）
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
# 依赖直装而非 pip install .，避免把源码复制两次
RUN pip install --no-cache-dir \
      "django>=5.1,<6.0" "psycopg[binary]>=3.2" "pydantic>=2.9" \
      "structlog>=24.4" "httpx>=0.27" "gunicorn>=23.0"

COPY . .

# 非 root 运行；/data 放原始页（7 天，可丢）
RUN useradd --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data/raw /app/.data \
 && chown -R app:app /app /data
USER app

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "4", "--access-logfile", "-"]
