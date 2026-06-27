"""Функции для работы с таблицей ``chats`` — кэш чатов MAX.

MAX-процесс обновляет кэш в двух сценариях:

* ``on_start`` (после успешного login) — синхронизирует весь список
  чатов через ``fetch_chats``.
* ``on_chat_update`` — обновляет запись при любой активности в чате
  (новое сообщение, переименование и т.п.).

Бот читает кэш через ``list_chats`` (команда ``/chats``).
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import select

from shared.db._engine import session_scope
from shared.db._models import Chat


def upsert_chat(chat: dict) -> None:
    """Вставить или обновить запись о MAX-чате."""
    with session_scope() as s:
        existing = s.execute(
            select(Chat).where(Chat.max_chat_id == chat["max_chat_id"])
        ).scalar_one_or_none()
        if existing:
            existing.title = chat.get("title", existing.title)
            existing.type = chat.get("type", existing.type)
            existing.last_preview = chat.get("last_message_preview", existing.last_preview)
            existing.last_ts = chat.get("last_message_at", existing.last_ts)
            existing.unread = chat.get("unread", existing.unread)
            existing.updated_at = datetime.utcnow()
        else:
            s.add(
                Chat(
                    max_chat_id=chat["max_chat_id"],
                    title=chat.get("title"),
                    type=chat.get("type"),
                    last_preview=chat.get("last_message_preview"),
                    last_ts=chat.get("last_message_at"),
                    unread=chat.get("unread"),
                )
            )


def list_chats(limit: int = 100) -> List[Chat]:
    """Список MAX-чатов для бота (``/chats``). Сортировка по ``last_ts``."""
    with session_scope() as s:
        rows = (
            s.execute(select(Chat).order_by(Chat.last_ts.desc().nullslast()).limit(limit))
            .scalars()
            .all()
        )
        s.expunge_all()
        return list(rows)


def get_chat(max_chat_id: str) -> Optional[Chat]:
    """Вернуть одну запись о MAX-чате по ``max_chat_id`` или ``None``.

    Используется ботом (``forwarder.py``) чтобы достать ``chat.type``
    (DIALOG/CHAT/CHANNEL) для формирования имени топика. Если записи
    ещё нет (MAX-процесс не успел синхронизировать кеш) — возвращает
    ``None``, и вызывающий код использует старый формат ``(MAX: <id>)``.
    """
    with session_scope() as s:
        row = (
            s.execute(
                select(Chat).where(Chat.max_chat_id == str(max_chat_id))
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        s.expunge(row)
        return row
