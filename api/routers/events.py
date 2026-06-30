"""Роутер событий MAX: ``POST/GET/DELETE /events``.

Поток данных:

1. ``POST /events`` — MAX-процесс кладёт новое сообщение (``upsert_event``).
2. ``GET /events?undelivered=true`` — ``EventPoller`` в боте забирает
   партию для доставки в Telegram-топики.
3. ``GET /events/{id}`` — callback'и бота (reply/showid/history) достают
   ``max_chat_id`` события.
4. ``POST /events/{id}/delivered`` — бот подтверждает доставку в Telegram.
5. ``GET /events/by-chat/{chat_id}`` — ``/history`` и callback «🔄 История».

Все эндпоинты защищены ``X-Api-Key`` (см. ``shared.api_auth.verify_api_key``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

from shared import db
from shared.api_auth import verify_api_key

from api.routers.schemas import EventIn, EventOut, OkOut, TgMappingIn

logger = logging.getLogger(__name__)
router = APIRouter()


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


@router.post("/events", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def post_event(event: EventIn) -> OkOut:
    payload = event.model_dump()
    if payload.get("timestamp"):
        try:
            payload["timestamp"] = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
        except ValueError:
            payload["timestamp"] = None
    db.upsert_event(payload)
    return OkOut(ok=True)


@router.get("/events", response_model=List[EventOut], dependencies=[Depends(verify_api_key)])
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


@router.get("/events/{event_id}", response_model=EventOut, dependencies=[Depends(verify_api_key)])
def get_event(event_id: int) -> EventOut:
    """Получить одно событие по id (нужно бота-колбэкам reply/showid/history,
    которые получают только короткий ``event_id`` в callback_data из-за
    64-байтного лимита Telegram Bot API на ``callback_data``).
    """
    with db.session_scope() as s:
        row = s.get(db.Event, event_id)
        if not row:
            raise HTTPException(status_code=404, detail="event not found")
        s.expunge(row)
        return _event_to_out(row)


@router.get("/events/by-chat/{chat_id}", response_model=List[EventOut], dependencies=[Depends(verify_api_key)])
def events_by_chat(chat_id: str, limit: int = Query(default=20, ge=1, le=200)) -> List[EventOut]:
    rows = db.list_events_for_chat(chat_id, limit=limit)
    return [_event_to_out(r) for r in rows]


@router.post("/events/{event_id}/delivered", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def mark_event_delivered(event_id: int) -> OkOut:
    db.mark_event_delivered(event_id)
    # Параллельно записываем в ``delivered_messages`` — это источник истины
    # для пометки прочитанным в MAX. Делаем ``best effort``: если событие
    # не найдено в ``events``, пропускаем.
    try:
        with db.session_scope() as s:
            row = s.get(db.Event, event_id)
            if row is not None:
                db.record_delivered(row.max_chat_id, row.max_message_id)
    except Exception as exc:
        logger.warning("record_delivered for event %s failed: %s", event_id, exc)
    return OkOut(ok=True)


@router.post(
    "/events/{event_id}/tg-mapping",
    response_model=OkOut,
    dependencies=[Depends(verify_api_key)],
)
def post_event_tg_mapping(event_id: int, body: TgMappingIn) -> OkOut:
    """Сохранить обратную TG-ссылку для события из MAX.

    Вызывается из :class:`bot.app.forwarder.EventPoller` сразу после
    успешной отправки сообщения в TG: ``tg_chat_id`` / ``tg_thread_id``
    / ``tg_message_id`` нужны двусторонней синхронизации реакций
    (``MessageReactionUpdated`` → ``max_chat_id``/``max_message_id`` и
    наоборот, для ``setMessageReaction``).

    Best-effort: если событие не найдено в ``events``, обновление
    ``delivered_messages`` всё равно проходит — TG-ссылка нужна для
    будущих реакций, даже если запись события удалили.
    """
    try:
        with db.session_scope() as s:
            row = s.get(db.Event, event_id)
            if row is not None:
                db.record_delivered_with_tg(
                    row.max_chat_id,
                    row.max_message_id,
                    tg_chat_id=body.tg_chat_id,
                    tg_thread_id=body.tg_thread_id,
                    tg_message_id=body.tg_message_id,
                )
    except Exception as exc:
        logger.warning(
            "record_delivered_with_tg for event %s failed: %s", event_id, exc,
        )
    return OkOut(ok=True)
