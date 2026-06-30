"""Функции для пометки «прочитано в TG» → MAX.

Двухтабличная логика:

* ``DeliveredMessage`` — факт доставки конкретного сообщения из MAX в
  Telegram-бот. Заполняется при ``POST /events/{id}/delivered``.
* ``ChatReadState`` — время последнего «прочтения» чата пользователем.
  Обновляется при любом действии пользователя (``REPLY``, ``SHOWID``,
  ввод текста через ``/reply`` и т.п.).

MAX-процесс периодически забирает ``get_pending_read_receipts`` —
список сообщений с ``delivered_at <= chat.last_read_at`` и ``read_at IS NULL``
— и помечает их прочитанными через ``client.read_message``. После успеха
вызывает ``mark_delivered_as_read``.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import select

from shared.db._engine import session_scope
from shared.db._models import ChatReadState, DeliveredMessage


def record_delivered(max_chat_id: str, max_message_id: str) -> None:
    """Записать факт доставки сообщения из MAX в TG-бот.

    Идемпотентно: если запись уже есть, обновляем ``delivered_at``.
    """
    record_delivered_with_tg(max_chat_id, max_message_id, None, None, None)


def record_delivered_with_tg(
    max_chat_id: str,
    max_message_id: str,
    tg_chat_id: Optional[int] = None,
    tg_thread_id: Optional[int] = None,
    tg_message_id: Optional[int] = None,
) -> None:
    """Записать факт доставки + обратную TG-ссылку.

    Используется в :class:`bot.app.forwarder.EventPoller` после успешной
    отправки сообщения в Telegram: ``tg_chat_id`` / ``tg_thread_id`` /
    ``tg_message_id`` нужны двусторонней синхронизации реакций
    (``MessageReactionUpdated`` → ``max_chat_id``/``max_message_id`` и
    наоборот, для ``setMessageReaction``).

    Идемпотентно: если запись уже есть, обновляет ``delivered_at`` и
    TG-идентификаторы. Если новые TG-идентификаторы не переданы (None),
    существующие значения сохраняются.
    """
    with session_scope() as s:
        existing = (
            s.query(DeliveredMessage)
            .filter(
                DeliveredMessage.max_chat_id == str(max_chat_id),
                DeliveredMessage.max_message_id == str(max_message_id),
            )
            .first()
        )
        if existing:
            existing.delivered_at = datetime.utcnow()
            if tg_chat_id is not None:
                existing.tg_chat_id = int(tg_chat_id)
            if tg_thread_id is not None:
                existing.tg_thread_id = int(tg_thread_id)
            if tg_message_id is not None:
                existing.tg_message_id = int(tg_message_id)
            s.flush()
            return
        s.add(DeliveredMessage(
            max_chat_id=str(max_chat_id),
            max_message_id=str(max_message_id),
            delivered_at=datetime.utcnow(),
            tg_chat_id=int(tg_chat_id) if tg_chat_id is not None else None,
            tg_thread_id=int(tg_thread_id) if tg_thread_id is not None else None,
            tg_message_id=int(tg_message_id) if tg_message_id is not None else None,
        ))


def set_summary_message_id(
    max_chat_id: str,
    max_message_id: str,
    summary_message_id: int,
) -> None:
    """Сохранить ``tg_summary_message_id`` для записи доставки.

    Используется :class:`bot.app.handlers.reactions_max.ReactionsMaxPoller`
    при создании/обновлении сообщения-сводки «👍×N 🔥×M · итого K»
    под входящим из MAX сообщением (только для CHAT/CHANNEL).
    """
    with session_scope() as s:
        row = (
            s.query(DeliveredMessage)
            .filter(
                DeliveredMessage.max_chat_id == str(max_chat_id),
                DeliveredMessage.max_message_id == str(max_message_id),
            )
            .first()
        )
        if row is None:
            return
        row.tg_summary_message_id = int(summary_message_id)


def update_chat_read_state(max_chat_id: str, read_at: Optional[datetime] = None) -> None:
    """Обновить ``last_read_at`` для чата (вызывается при любом действии пользователя).

    Идемпотентно: только увеличивает ``last_read_at`` (никогда не уменьшает).
    """
    when = read_at or datetime.utcnow()
    with session_scope() as s:
        existing = (
            s.query(ChatReadState)
            .filter(ChatReadState.max_chat_id == str(max_chat_id))
            .first()
        )
        if existing:
            if when > existing.last_read_at:
                existing.last_read_at = when
        else:
            s.add(ChatReadState(
                max_chat_id=str(max_chat_id),
                last_read_at=when,
            ))


def get_pending_read_receipts(limit: int = 100) -> List[DeliveredMessage]:
    """MAX-процесс забирает все доставленные, но ещё не помеченные
    прочитанными сообщения, у которых ``delivered_at <= min(chat.last_read_at)``.
    Возвращает ``DeliveredMessage`` с непустым ``read_at = None``.
    """
    with session_scope() as s:
        rows = (
            s.execute(
                select(DeliveredMessage)
                .join(
                    ChatReadState,
                    DeliveredMessage.max_chat_id == ChatReadState.max_chat_id,
                    isouter=False,
                )
                .where(
                    DeliveredMessage.read_at.is_(None),
                    DeliveredMessage.delivered_at <= ChatReadState.last_read_at,
                )
                .order_by(DeliveredMessage.delivered_at.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        s.expunge_all()
        return list(rows)


def mark_delivered_as_read(delivered_id: int) -> None:
    """MAX-процесс вызывает после успешного ``client.read_message``.

    Проставляет ``read_at = now()`` — повторно брать эту запись не будем.
    """
    with session_scope() as s:
        row = s.get(DeliveredMessage, delivered_id)
        if not row:
            return
        row.read_at = datetime.utcnow()


def get_delivered_by_max_message(
    max_chat_id: str, max_message_id: str
) -> Optional[DeliveredMessage]:
    """Найти запись о доставке по ``max_chat_id`` / ``max_message_id``.

    Используется :class:`bot.app.handlers.reactions_max.ReactionsMaxPoller`
    для сводок: задача ``to_tg_summary`` приходит только с MAX-идами,
    TG-ссылку достаём из ``delivered_messages``.
    """
    with session_scope() as s:
        row = (
            s.query(DeliveredMessage)
            .filter(
                DeliveredMessage.max_chat_id == str(max_chat_id),
                DeliveredMessage.max_message_id == str(max_message_id),
            )
            .first()
        )
        if not row:
            return None
        s.expunge(row)
        return row


def get_delivered_by_tg_message(
    tg_chat_id: int, tg_message_id: int
) -> Optional[DeliveredMessage]:
    """Найти запись о доставке по ``tg_chat_id`` / ``tg_message_id``.

    Используется при обработке ``MessageReactionUpdated``: TG-апдейт
    даёт нам ``tg_message_id``, а ``max_chat_id`` / ``max_message_id``
    достаём из этой таблицы (заполняется в ``EventPoller`` через
    ``POST /events/{id}/tg-mapping``).
    """
    with session_scope() as s:
        row = (
            s.query(DeliveredMessage)
            .filter(
                DeliveredMessage.tg_chat_id == int(tg_chat_id),
                DeliveredMessage.tg_message_id == int(tg_message_id),
            )
            .first()
        )
        if not row:
            return None
        s.expunge(row)
        return row