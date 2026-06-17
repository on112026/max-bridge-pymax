"""FastAPI-приложение моста MAX ↔ Telegram (этап 2, PyMax).

Без VNC, без headful-прокси, без watcher'а — все 2FA/SMS происходят
через PyMax SmsAuthFlow, а коды владелец вводит в Telegram-боте.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Подключаем /app/shared, /app/api как путь импорта (контейнерная раскладка)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "api"))

from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from shared import db, models
from shared.api_auth import verify_api_key
from shared.config import load_settings
from shared.log_setup import configure_logging


settings = load_settings()
configure_logging(settings.log_level)
db.init_engine(settings.db_path)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_engine(settings.db_path)
    os.makedirs(settings.media_dir, exist_ok=True)
    yield


app = FastAPI(title="MAX ↔ Telegram Bridge API (PyMax)", version="2.0.0", lifespan=lifespan)


# ---------- Схемы запросов/ответов ----------


class EventIn(BaseModel):
    max_chat_id: str
    max_message_id: str
    chat_title: Optional[str] = None
    sender: Optional[str] = None
    sender_id: Optional[str] = None
    text: Optional[str] = None
    kind: str = "text"
    media_path: Optional[str] = None
    media_mime: Optional[str] = None
    media_filename: Optional[str] = None
    media_size: Optional[int] = None
    timestamp: Optional[str] = None
    is_outgoing: bool = False


class EventOut(BaseModel):
    id: int
    max_chat_id: str
    max_message_id: str
    chat_title: Optional[str] = None
    sender: Optional[str] = None
    sender_id: Optional[str] = None
    text: Optional[str] = None
    kind: str
    media_path: Optional[str] = None
    media_mime: Optional[str] = None
    media_filename: Optional[str] = None
    media_size: Optional[int] = None
    timestamp: Optional[str] = None
    is_outgoing: bool


class ChatIn(BaseModel):
    max_chat_id: str
    title: Optional[str] = None
    type: Optional[str] = None
    last_message_preview: Optional[str] = None
    last_message_at: Optional[str] = None
    unread: Optional[int] = None


class ChatOut(BaseModel):
    max_chat_id: str
    title: Optional[str] = None
    type: Optional[str] = None
    last_message_preview: Optional[str] = None
    last_message_at: Optional[str] = None
    unread: Optional[int] = None


class SendIn(BaseModel):
    kind: str = "text"
    target_chat_id: str
    text: Optional[str] = None
    media_path: Optional[str] = None
    media_mime: Optional[str] = None
    media_filename: Optional[str] = None
    created_by: Optional[int] = None


class SendOut(BaseModel):
    id: int
    kind: str
    target_chat_id: str
    text: Optional[str] = None
    media_path: Optional[str] = None
    media_mime: Optional[str] = None
    media_filename: Optional[str] = None
    status: str
    error: Optional[str] = None
    created_at: Optional[str] = None
    finished_at: Optional[str] = None


class StatusOut(BaseModel):
    auth: dict
    queue: dict
    undelivered: int
    chats: int


class OkOut(BaseModel):
    ok: bool = True


def _event_to_out(e) -> EventOut:
    return EventOut(
        id=e.id,
        max_chat_id=e.max_chat_id,
        max_message_id=e.max_message_id,
        chat_title=e.chat_title,
        sender=e.sender,
        sender_id=e.sender_id,
        text=e.text,
        kind=e.kind,
        media_path=e.media_path,
        media_mime=e.media_mime,
        media_filename=e.media_filename,
        media_size=e.media_size,
        timestamp=e.ts.isoformat() if e.ts else None,
        is_outgoing=e.is_outgoing,
    )


def _chat_to_out(c) -> ChatOut:
    return ChatOut(
        max_chat_id=c.max_chat_id,
        title=c.title,
        type=c.type,
        last_message_preview=c.last_preview,
        last_message_at=c.last_ts.isoformat() if c.last_ts else None,
        unread=c.unread,
    )


def _send_to_out(s) -> SendOut:
    return SendOut(
        id=s.id,
        kind=s.kind,
        target_chat_id=s.target_chat_id,
        text=s.text,
        media_path=s.media_path,
        media_mime=s.media_mime,
        media_filename=s.media_filename,
        status=s.status,
        error=s.error,
        created_at=s.created_at.isoformat() if s.created_at else None,
        finished_at=s.finished_at.isoformat() if s.finished_at else None,
    )


# ---------- Маршруты ----------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def root() -> dict:
    return {"service": "max-bridge-pymax-api", "version": "2.0.0"}


@app.post("/events", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def post_event(event: EventIn) -> OkOut:
    payload = event.model_dump()
    if payload.get("timestamp"):
        from datetime import datetime
        try:
            payload["timestamp"] = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
        except ValueError:
            payload["timestamp"] = None
    db.upsert_event(payload)
    return OkOut(ok=True)


@app.get("/events", response_model=List[EventOut], dependencies=[Depends(verify_api_key)])
def list_events(
    undelivered: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=200),
) -> List[EventOut]:
    if undelivered:
        rows = db.list_undelivered_events(limit=limit)
    else:
        with db.session_scope() as s:
            from sqlalchemy import select
            rows = (
                s.execute(select(db.Event).order_by(db.Event.ts.desc()).limit(limit))
                .scalars()
                .all()
            )
            s.expunge_all()
            rows = list(rows)
    return [_event_to_out(r) for r in rows]


@app.get("/events/by-chat/{chat_id}", response_model=List[EventOut], dependencies=[Depends(verify_api_key)])
def events_by_chat(chat_id: str, limit: int = Query(default=20, ge=1, le=200)) -> List[EventOut]:
    rows = db.list_events_for_chat(chat_id, limit=limit)
    return [_event_to_out(r) for r in rows]


@app.post("/events/{event_id}/delivered", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def mark_event_delivered(event_id: int) -> OkOut:
    db.mark_event_delivered(event_id)
    return OkOut(ok=True)


@app.post("/chats", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def post_chat(chat: ChatIn) -> OkOut:
    payload = chat.model_dump()
    if payload.get("last_message_at"):
        from datetime import datetime
        try:
            payload["last_message_at"] = datetime.fromisoformat(payload["last_message_at"].replace("Z", "+00:00"))
        except ValueError:
            payload["last_message_at"] = None
    db.upsert_chat(payload)
    return OkOut(ok=True)


@app.get("/chats", response_model=List[ChatOut], dependencies=[Depends(verify_api_key)])
def get_chats(limit: int = Query(default=100, ge=1, le=500)) -> List[ChatOut]:
    rows = db.list_chats(limit=limit)
    return [_chat_to_out(r) for r in rows]


@app.post("/send", response_model=SendOut, dependencies=[Depends(verify_api_key)])
def post_send(item: SendIn) -> SendOut:
    item_id = db.enqueue_send(item.model_dump())
    with db.session_scope() as s:
        row = s.get(db.SendQueue, item_id)
        s.expunge(row)
        return _send_to_out(row)


@app.get("/send/next", response_model=Optional[SendOut], dependencies=[Depends(verify_api_key)])
def get_next_send() -> Optional[SendOut]:
    row = db.claim_next_send()
    if not row:
        return None
    return _send_to_out(row)


@app.post("/send/{item_id}/finish", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def finish_send(item_id: int, ok: bool = True, error: Optional[str] = None) -> OkOut:
    db.finish_send(item_id, ok=ok, error=error)
    return OkOut(ok=True)


@app.get("/send/{item_id}", response_model=Optional[SendOut], dependencies=[Depends(verify_api_key)])
def get_send(item_id: int) -> Optional[SendOut]:
    with db.session_scope() as s:
        row = s.get(db.SendQueue, item_id)
        if not row:
            return None
        s.expunge(row)
        return _send_to_out(row)


@app.get("/status", response_model=StatusOut, dependencies=[Depends(verify_api_key)])
def get_status() -> StatusOut:
    return StatusOut(
        auth=db.get_auth_state(),
        queue=db.queue_stats(),
        undelivered=len(db.list_undelivered_events(limit=1000)),
        chats=len(db.list_chats(limit=1000)),
    )


# ---------- Auth state & 2FA/пароль от владельца (вводятся через Telegram-бота) ----------


class AuthStateIn(BaseModel):
    status: str
    error: Optional[str] = None
    # Если True — ``last_error`` сбрасывается в NULL даже при ``error=None``.
    # Нужно max-процессу, чтобы «очистить» предыдущую ошибку при status=ok
    # или при ручном reauth (если раньше была rate-limit-ошибка).
    clear_error: bool = False


@app.post("/auth/state", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def post_auth_state(body: AuthStateIn) -> OkOut:
    db.set_auth_state(
        body.status,
        error=body.error,
        last_login=body.status == "ok",
        clear_error=body.clear_error,
    )
    return OkOut(ok=True)


class TwoFaRequestIn(BaseModel):
    kind: str = "sms"  # "sms" | "password"


class TwoFaRequestOut(BaseModel):
    request_id: int
    kind: str


@app.post("/auth/2fa/request", response_model=TwoFaRequestOut, dependencies=[Depends(verify_api_key)])
def post_2fa_request(body: Optional[TwoFaRequestIn] = None) -> TwoFaRequestOut:
    """max-процесс вызывает, когда PyMax SmsAuthFlow запросил SMS-код или пароль.

    ``kind`` может быть ``"sms"`` (по умолчанию) или ``"password"`` — чтобы
    Telegram-бот отправлял владельцу разные подсказки.
    """
    kind = (body.kind if body else "sms") or "sms"
    if kind not in ("sms", "password"):
        kind = "sms"
    rid = db.open_2fa_request(kind=kind)
    logger.info("2fa request opened: id=%s kind=%s", rid, kind)
    return TwoFaRequestOut(request_id=rid, kind=kind)


class TwoFaCodeIn(BaseModel):
    request_id: int
    code: str


@app.post("/auth/2fa", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def post_2fa(body: TwoFaCodeIn) -> OkOut:
    """Telegram-бот кладёт сюда код/пароль, введённый владельцем."""
    db.put_2fa_code(body.request_id, body.code)
    return OkOut(ok=True)


class TwoFaCodeOut(BaseModel):
    code: Optional[str] = None


@app.get("/auth/2fa/peek/{request_id}", response_model=TwoFaCodeOut, dependencies=[Depends(verify_api_key)])
def peek_2fa(request_id: int) -> TwoFaCodeOut:
    """max-процесс опрашивает этот эндпоинт, чтобы забрать введённый код/пароль."""
    code = db.take_pending_2fa_code(request_id)
    if code is not None:
        db.clear_2fa_request()
    return TwoFaCodeOut(code=code)


# ---------- Внутренние уведомления (опционально, для отладки) ----------


class NotifyIn(BaseModel):
    """Используется max-процессом, чтобы сообщить api о системных событиях
    (например, «пришёл запрос на SMS»). Сейчас api сам опрашивает auth_state,
    поэтому этот маршрут — no-op, оставлен на будущее (push вместо polling)."""

    event: str
    payload: Optional[dict] = None


@app.post("/internal/notify", dependencies=[Depends(verify_api_key)])
def internal_notify(body: NotifyIn) -> OkOut:
    logger.info("internal_notify: %s %s", body.event, body.payload)
    return OkOut(ok=True)