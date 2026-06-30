"""FastAPI-приложение моста MAX ↔ Telegram (этап 2, PyMax).

Это **точка сборки**: здесь создаётся ``FastAPI(app)``, настраивается
``lifespan`` для инициализации БД, подключаются все роутеры из
``api/routers/``. Логика эндпоинтов живёт в отдельных модулях пакета
``api/routers/`` (по доменам: events, chats, send, status, auth,
sessions, topic_jobs, topics, sync, health).

Без VNC, без headful-прокси, без watcher'а — все 2FA/SMS происходят
через PyMax SmsAuthFlow, а коды владелец вводит в Telegram-боте.

Модель авторизации «только по команде»: на cold-start supervisor ставит
``auth_state.status = auth_required`` и НЕ создаёт PyMax Client. Владелец
через бота нажимает inline-кнопку, и только тогда бот кладёт
``pending_action`` в БД (эндпоинт ``/auth/action``), а supervisor
обрабатывает его на следующей итерации.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Подключаем /app/shared, /app/api как путь импорта (контейнерная раскладка)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "api"))

from fastapi import FastAPI

logger = logging.getLogger(__name__)

from shared import db  # noqa: E402
from shared.config import load_settings  # noqa: E402
from shared.log_setup import configure_logging  # noqa: E402

from api.routers import (  # noqa: E402
    auth,
    chat_ops,
    chats,
    events,
    health,
    reaction_ops,
    send,
    sessions,
    status,
    sync,
    topic_jobs,
    topics,
)

settings = load_settings()
configure_logging(settings.log_level)
db.init_engine(settings.db_path)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_engine(settings.db_path)
    os.makedirs(settings.media_dir, exist_ok=True)
    yield


app = FastAPI(title="MAX ↔ Telegram Bridge API (PyMax)", version="2.0.0", lifespan=lifespan)

# ---------- Подключение роутеров ----------
app.include_router(health.router)
app.include_router(events.router)
app.include_router(chats.router)
app.include_router(send.router)
app.include_router(status.router)
app.include_router(auth.router)
app.include_router(sessions.router)
app.include_router(topic_jobs.router)
app.include_router(topics.router)
app.include_router(sync.router)
app.include_router(chat_ops.router)
app.include_router(reaction_ops.router)
