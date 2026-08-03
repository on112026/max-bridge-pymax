"""Функции для работы с таблицей ``events`` — входящие события из MAX.

Мост MAX → Telegram работает так:

1. MAX-процесс кладёт новое сообщение через ``upsert_event`` (или API
   вызывает ``POST /events``). Дубль по ``(max_chat_id, max_message_id)``
   игнорируется.
2. Бот-процесс забирает ``list_undelivered_events`` через ``EventPoller``
   и пересылает в Telegram-топики. После успешной доставки вызывает
   ``mark_event_delivered``.
3. История (``/history``, кнопка «🔄 История») — ``list_events_for_chat``.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import select

from shared.db._engine import session_scope
from shared.db._models import Event


def upsert_event(event: dict) -> Optional[int]:
    """Вставляет новое событие; возвращает id, либо None если дубль."""
    with session_scope() as s:
        existing = s.execute(
            select(Event).where(
                Event.max_chat_id == event["max_chat_id"],
                Event.max_message_id == event["max_message_id"],
            )
        ).scalar_one_or_none()
        if existing:
            return None
        e = Event(
            max_chat_id=event["max_chat_id"],
            max_message_id=event["max_message_id"],
            chat_title=event.get("chat_title"),
            sender=event.get("sender"),
            sender_id=event.get("sender_id"),
            text=event.get("text"),
            kind=event.get("kind", "text"),
            media_path=event.get("media_path"),
            media_mime=event.get("media_mime"),
            media_filename=event.get("media_filename"),
            media_size=event.get("media_size"),
            ts=event.get("timestamp") or datetime.utcnow(),
            is_outgoing=event.get("is_outgoing", False),
            delivered=False,
            raw_json=event.get("raw_json"),
        )
        s.add(e)
        s.flush()
        return e.id


def update_event_text(max_chat_id: str, max_message_id: str, new_text: str) -> Optional[int]:
    """Обновить текст уже сохранённого события (редактирование сообщения в MAX).

    Выставляет text_edited_at = now() как флаг для EditWorker в боте.
    Если событие ещё не доставлено — текст обновится до отправки, флаг не нужен.
    """
    with session_scope() as s:
        existing = s.execute(
            select(Event).where(
                Event.max_chat_id == max_chat_id,
                Event.max_message_id == max_message_id,
            )
        ).scalar_one_or_none()
        if not existing:
            return None
        existing.text = new_text
        if existing.delivered:
            existing.text_edited_at = datetime.utcnow()
        s.flush()
        return existing.id


def list_pending_edits(limit: int = 50) -> list:
    """Список событий с text_edited_at IS NOT NULL (ждут edit_message_text в TG)."""
    with session_scope() as s:
        rows = (
            s.execute(
                select(Event)
                .where(Event.text_edited_at.isnot(None))
                .order_by(Event.text_edited_at.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        s.expunge_all()
        return list(rows)


def clear_edit_flag(event_id: int) -> None:
    """Сбросить флаг text_edited_at после успешного edit_message_text в TG."""
    with session_scope() as s:
        e = s.get(Event, event_id)
        if e:
            e.text_edited_at = None


def mark_event_delivered(event_id: int) -> None:
    """Пометить событие доставленным в Telegram."""
    with session_scope() as s:
        e = s.get(Event, event_id)
        if not e:
            return
        e.delivered = True
        e.delivered_at = datetime.utcnow()


def list_undelivered_events(limit: int = 50) -> List[Event]:
    """Список недоставленных событий (для ``EventPoller``)."""
    with session_scope() as s:
        rows = (
            s.execute(
                select(Event)
                .where(Event.delivered.is_(False))
                .order_by(Event.ts.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        s.expunge_all()
        return list(rows)


def list_events_for_chat(max_chat_id: str, limit: int = 20) -> List[Event]:
    """Последние ``limit`` событий для конкретного MAX-чата (для ``/history``)."""
    with session_scope() as s:
        rows = (
            s.execute(
                select(Event)
                .where(Event.max_chat_id == max_chat_id)
                .order_by(Event.ts.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        s.expunge_all()
        return list(rows)