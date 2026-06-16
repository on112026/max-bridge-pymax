# syntax=docker/dockerfile:1
# MAX → Telegram bridge (этап 2, без Playwright/Chromium/VNC).
# Лёгкий образ: только Python + PyMax + supervisord.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

# Системные зависимости. PyMax работает по TCP/WS — не нужен chromium, x11vnc, novnc и т.п.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        supervisor \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1) Сначала вендор + зависимости, чтобы кэшировался слой pip.
COPY vendor/ ./vendor/
COPY requirements.txt ./requirements.txt
COPY api/requirements.txt ./api/requirements.txt
COPY bot/requirements.txt ./bot/requirements.txt
COPY max/requirements.txt ./max/requirements.txt

RUN pip install --no-cache-dir \
        -r requirements.txt \
        -r api/requirements.txt \
        -r bot/requirements.txt \
        -r max/requirements.txt

# 2) Код проекта.
COPY shared/ ./shared/
COPY api/    ./api/
COPY bot/    ./bot/
COPY max/    ./max/
# Главный supervisord-конфиг кладём напрямую (наш supervisord.conf — это и есть
# «главный» файл, никаких conf.d/*.conf нам не нужно).
COPY supervisord.conf /etc/supervisor/supervisord.conf

# Гарантируем, что нужные каталоги существуют (тома /data и /app/cache).
RUN mkdir -p /data/media/inbox /app/cache

EXPOSE 8000

# VOLUME ["/data", "/app/cache"]

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
