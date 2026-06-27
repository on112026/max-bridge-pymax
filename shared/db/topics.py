"""Функции для работы с таблицей ``chat_topics`` — связки MAX-чат ↔ Telegram-топик.

Каждый уникальный ``max_chat_id`` получает свой топик внутри supergroup.
Топики создаются автоматически при первом входящем сообщении из MAX
(``forwarder.py::get_or_create_topic`` → ``topics.get_or_create_topic``).

``stale`` — флаг «MAX-чат пропал»:

* ``0`` — живой чат, топик актуален (по умолчанию).
* ``1`` — MAX-чат не найден в свежем sync (``fetch_chats``), но топик
  в Telegram ещё открыт. Показываем в ``/status`` и предлагаем ``/prune_topics``.
* ``2`` — владелец явно закрыл топик (``closeForumTopic``) или пометил
  его как устаревший; больше не показываем.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import select, update

from shared.db._engine import session_scope
from shared.db._models import ChatTopic, SuperGroup


def get_topic(max_chat_id: str) -> Optional[ChatTopic]:
    """Возвращает топик для ``max_chat_id`` или ``None``."""
    with session_scope() as s:
        row = (
            s.query(ChatTopic)
            .filter(ChatTopic.max_chat_id == str(max_chat_id))
            .first()
        )
        if not row:
            return None
        s.expunge(row)
        return row


def get_topic_by_thread_id(
    supergroup_chat_id: int, thread_id: int
) -> Optional[ChatTopic]:
    """Обратный lookup: топик по ``(supergroup_chat_id, thread_id)``.

    Используется «эхо»-хэндлером бота, который при получении сообщения
    в топике супергруппы должен понять, в какой MAX-чат его отправить.
    Возвращает ``None``, если такого топика нет.
    """
    with session_scope() as s:
        row = (
            s.query(ChatTopic)
            .filter(
                ChatTopic.supergroup_chat_id == int(supergroup_chat_id),
                ChatTopic.thread_id == int(thread_id),
            )
            .first()
        )
        if not row:
            return None
        s.expunge(row)
        return row


def create_topic(
    max_chat_id: str,
    supergroup_chat_id: int,
    thread_id: int,
    topic_name: Optional[str] = None,
) -> None:
    """Создать запись о топике. Идемпотентно: если уже есть — не трогает."""
    with session_scope() as s:
        existing = (
            s.query(ChatTopic)
            .filter(ChatTopic.max_chat_id == str(max_chat_id))
            .first()
        )
        if existing:
            return
        s.add(ChatTopic(
            max_chat_id=str(max_chat_id),
            supergroup_chat_id=int(supergroup_chat_id),
            thread_id=int(thread_id),
            topic_name=topic_name,
        ))


def update_topic_name(max_chat_id: str, topic_name: str) -> None:
    """Обновить ``topic_name`` (например, при ``on_chat_update`` из MAX)."""
    with session_scope() as s:
        row = (
            s.query(ChatTopic)
            .filter(ChatTopic.max_chat_id == str(max_chat_id))
            .first()
        )
        if row and row.topic_name != topic_name:
            row.topic_name = topic_name


def list_topics() -> List[ChatTopic]:
    """Все топики (используется при старте для логов)."""
    with session_scope() as s:
        rows = (
            s.query(ChatTopic)
            .order_by(ChatTopic.created_at.asc())
            .all()
        )
        s.expunge_all()
        return list(rows)


def mark_topics_stale(missing_max_chat_ids: List[str]) -> int:
    """Пометить ``stale=1`` для всех топиков, чей ``max_chat_id`` отсутствует
    в свежем sync из MAX.

    Идемпотентно: топики, которые уже ``stale=2`` (закрыты вручную), НЕ
    трогаем — иначе после закрытия они снова всплывут как «stale».

    Возвращает количество помеченных строк.
    """
    if not missing_max_chat_ids:
        return 0
    with session_scope() as s:
        result = s.execute(
            update(ChatTopic)
            .where(
                ChatTopic.max_chat_id.in_([str(c) for c in missing_max_chat_ids]),
                ChatTopic.stale == 0,
            )
            .values(stale=1, updated_at=datetime.utcnow())
        )
        return int(result.rowcount or 0)


def unmark_topic_stale(max_chat_id: str) -> None:
    """Сбросить ``stale=0`` (MAX-чат снова появился в sync)."""
    with session_scope() as s:
        row = (
            s.query(ChatTopic)
            .filter(ChatTopic.max_chat_id == str(max_chat_id))
            .first()
        )
        if row and row.stale != 0:
            row.stale = 0
            row.updated_at = datetime.utcnow()


def list_stale_topics(owner_user_id: int) -> List[ChatTopic]:
    """Stale-топики (``stale=1``) для конкретного владельца.

    Используется командой ``/prune_topics``: показываем владельцу список
    топиков, у которых MAX-чат пропал, чтобы он решил — закрыть или оставить.
    """
    with session_scope() as s:
        sg = (
            s.query(SuperGroup)
            .filter(SuperGroup.owner_user_id == int(owner_user_id))
            .first()
        )
        if not sg:
            return []
        rows = (
            s.query(ChatTopic)
            .filter(
                ChatTopic.supergroup_chat_id == int(sg.supergroup_chat_id),
                ChatTopic.stale == 1,
            )
            .order_by(ChatTopic.created_at.asc())
            .all()
        )
        s.expunge_all()
        return list(rows)


def mark_topic_closed(max_chat_id: str) -> None:
    """Пометить топик как закрытый владельцем (``stale=2``)."""
    with session_scope() as s:
        row = (
            s.query(ChatTopic)
            .filter(ChatTopic.max_chat_id == str(max_chat_id))
            .first()
        )
        if row:
            row.stale = 2
            row.updated_at = datetime.utcnow()


def count_stale_topics() -> int:
    """Общее количество stale-топиков (``stale=1``) по всем владельцам.

    Показываем в ``/status`` как предупреждение.
    """
    with session_scope() as s:
        return int(
            s.query(ChatTopic).filter(ChatTopic.stale == 1).count() or 0
        )


def count_topics_for_owner(owner_user_id: int) -> int:
    """Количество stale-топиков у конкретного владельца (для ``/status``)."""
    with session_scope() as s:
        sg = (
            s.query(SuperGroup)
            .filter(SuperGroup.owner_user_id == int(owner_user_id))
            .first()
        )
        if not sg:
            return 0
        return int(
            s.query(ChatTopic)
            .filter(
                ChatTopic.supergroup_chat_id == int(sg.supergroup_chat_id),
                ChatTopic.stale == 1,
            )
            .count()
            or 0
        )