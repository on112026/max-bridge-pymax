"""Роутер чатов MAX и пометок «прочитано в TG» → MAX.

Два независимых домена, объединённых одним файлом ради соседства в OpenAPI:

* ``/chats`` — кэш MAX-чатов. ``POST`` обновляет запись (MAX-процесс),
  ``GET`` — бот в ``/chats``.
* ``/chats/{id}/read-up-to`` — бот сообщает «пользователь прочитал чат».
* ``/chats/pending-reads`` — MAX-процесс забирает доставленные, но ещё
  не помеченные прочитанными сообщения.
* ``/chats/{chat_id}/messages/{message_id}/read`` — MAX-процесс сообщает
  об успешном ``client.read_message``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

from shared import db
from shared.api_auth import verify_api_key

from api.routers.schemas import (
    ChatIn,
    ChatOut,
    OkOut,
    PendingReadReceipt,
    ReadReceiptOk,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _chat_to_out(c) -> ChatOut:
    return ChatOut(
        max_chat_id=c.max_chat_id,
        title=c.title,
        type=c.type,
        last_message_preview=c.last_preview,
        last_message_at=c.last_ts.isoformat() if c.last_ts else None,
        unread=c.unread,
    )


# ---------- /chats — кэш чатов MAX ----------


@router.post("/chats", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def post_chat(chat: ChatIn) -> OkOut:
    payload = chat.model_dump()
    if payload.get("last_message_at"):
        try:
            payload["last_message_at"] = datetime.fromisoformat(payload["last_message_at"].replace("Z", "+00:00"))
        except ValueError:
            payload["last_message_at"] = None
    db.upsert_chat(payload)
    return OkOut(ok=True)


@router.get("/chats", response_model=List[ChatOut], dependencies=[Depends(verify_api_key)])
def get_chats(limit: int = Query(default=100, ge=1, le=500)) -> List[ChatOut]:
    rows = db.list_chats(limit=limit)
    return [_chat_to_out(r) for r in rows]


# ---------- Read receipts ----------


@router.post(
    "/chats/{chat_id}/read-up-to",
    response_model=OkOut,
    dependencies=[Depends(verify_api_key)],
)
def mark_chat_read_up_to(chat_id: str) -> OkOut:
    """Бот вызывает при любом действии пользователя (REPLY, SHOWID, ввод текста).

    Это значит «все сообщения этого чата до этого момента прочитаны».
    MAX-процесс заберёт доставленные сообщения с ``delivered_at <= now``
    через ``GET /chats/pending-reads`` и пометит их в MAX через ``client.read_message``.
    """
    db.update_chat_read_state(chat_id)
    return OkOut(ok=True)


@router.get(
    "/chats/pending-reads",
    response_model=List[PendingReadReceipt],
    dependencies=[Depends(verify_api_key)],
)
def get_pending_reads(limit: int = Query(default=50, ge=1, le=500)) -> List[PendingReadReceipt]:
    """MAX-процесс забирает список доставленных сообщений, которые уже можно
    пометить прочитанными (``delivered_at <= chat.last_read_at``, ``read_at IS NULL``).
    """
    rows = db.get_pending_read_receipts(limit=limit)
    return [
        PendingReadReceipt(
            id=r.id,
            max_chat_id=r.max_chat_id,
            max_message_id=r.max_message_id,
            delivered_at=r.delivered_at.isoformat() if r.delivered_at else "",
        )
        for r in rows
    ]


@router.post(
    "/chats/{chat_id}/messages/{message_id}/read",
    response_model=ReadReceiptOk,
    dependencies=[Depends(verify_api_key)],
)
def mark_message_read(chat_id: str, message_id: str, delivered_id: int = Query(default=0)) -> ReadReceiptOk:
    """MAX-процесс вызывает после успешного ``client.read_message``.

    Проставляет ``read_at = now()`` для записи ``DeliveredMessage`` —
    чтобы больше её не брать.
    """
    if delivered_id > 0:
        db.mark_delivered_as_read(delivered_id)
        return ReadReceiptOk(ok=True, marked=1)
    # Фолбэк: ищем по (chat_id, message_id) и помечаем первую
    # непрочитанную запись.
    with db.session_scope() as s:
        from shared.db import DeliveredMessage
        row = (
            s.query(DeliveredMessage)
            .filter(
                DeliveredMessage.max_chat_id == str(chat_id),
                DeliveredMessage.max_message_id == str(message_id),
                DeliveredMessage.read_at.is_(None),
            )
            .order_by(DeliveredMessage.id.asc())
            .first()
        )
        if row is not None:
            row.read_at = datetime.utcnow()
            return ReadReceiptOk(ok=True, marked=1)
    return ReadReceiptOk(ok=True, marked=0)