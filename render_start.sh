#!/bin/sh
# render_start.sh — точка входа при деплое на Render.com
#
# Что делает:
#   1. Создаёт каталоги /data, /data/cache, /data/media
#   2. Скачивает bridge.db (основная БД) и сессию PyMax из Cloudflare D1
#   3. Запускает keep-alive loop (не даёт Render засыпать)
#   4. Запускает run_all.py

set -e

echo "[render_start] === max-bridge-pymax starting on Render.com ==="

# --- 1. Каталоги ---
mkdir -p /data/media/inbox /data/cache
echo "[render_start] directories ready"

# --- 2. Восстановление из Cloudflare D1 ---
if [ -n "$CF_ACCOUNT_ID" ] && [ -n "$CF_API_TOKEN" ] && [ -n "$CF_D1_DATABASE_ID" ]; then
    echo "[render_start] restoring from Cloudflare D1..."

    # Сначала создаём таблицу если её нет (идемпотентно)
    python3 /app/render_d1.py init && echo "[render_start] D1 table ready"

    # Скачиваем основную БД
    python3 /app/render_d1.py pull-db && true
    DB_STATUS=$?

    # Скачиваем сессию PyMax
    python3 /app/render_d1.py pull-session && true
    SESSION_STATUS=$?

    if [ -f "${DB_PATH:-/data/bridge.db}" ]; then
        DB_SIZE=$(wc -c < "${DB_PATH:-/data/bridge.db}")
        echo "[render_start] ✅ bridge.db restored (${DB_SIZE} bytes)"
    else
        echo "[render_start] ⚠️  bridge.db not in D1 — starting fresh (first run?)"
    fi

    SESSION_FILE="${CACHE_DIR:-/data/cache}/bridge"
    if [ -f "$SESSION_FILE" ]; then
        SESSION_SIZE=$(wc -c < "$SESSION_FILE")
        echo "[render_start] ✅ PyMax session restored (${SESSION_SIZE} bytes) — SMS not required"
    else
        echo "[render_start] ⚠️  PyMax session not in D1 — SMS auth will be requested"
    fi
else
    echo "[render_start] ⚠️  CF_ACCOUNT_ID / CF_API_TOKEN / CF_D1_DATABASE_ID not set"
    echo "[render_start]    data is ephemeral (lost on restart)"
    echo "[render_start]    set these vars to enable D1 persistence"
fi

# --- 3. Keep-alive ---
if [ -n "$RENDER_SERVICE_URL" ]; then
    (
        echo "[keep_alive] will ping ${RENDER_SERVICE_URL}/health every 10 minutes"
        sleep 45
        while true; do
            RESULT=$(python3 -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('${RENDER_SERVICE_URL}/health', timeout=10)
    print(r.read().decode()[:50])
except Exception as e:
    print('error:', e)
" 2>/dev/null)
            echo "[keep_alive] ping → $RESULT"
            sleep 600
        done
    ) &
    echo "[render_start] keep-alive started (PID=$!)"
else
    echo "[render_start] RENDER_SERVICE_URL not set — keep-alive disabled"
fi

# --- 4. Запуск ---
echo "[render_start] starting run_all.py..."
exec python3 /app/run_all.py
