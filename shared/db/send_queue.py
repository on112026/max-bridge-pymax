"""Функции для работы с таблицей ``send_queue`` — очередь отправки в MAX.

Жизненный цикл задачи:

1. Бот кладёт задачу через ``enqueue_send`` (``POST /send``).
2. MAX-процесс забирает ``claim_next_send`` (``GET /send/next``),
   переводит в ``in_progress`` и шлёт через ``client.send_message``.
3. MAX-процесс сообщает о результате через ``finish_send``
   (``POST /send/{id}/finish?ok=true&error=...``).

``thread_id`` — id TG-топика, из которого отправлено сообщение
(если пользователь писал из топика супергруппы). Сохраняется в
``SendQueue.thread_id`` для будущей синхронизации.

``tg_chat_id`` / ``tg_message_id`` — id TG-сообщения, из которого ушёл
ответ в MAX (``message.message_id`` в aiogram). После успешной отправки
в MAX (``client.send_message`` возвращает ``msg.id``) MAX-процесс создаёт
``DeliveredMessage``-строку, связывающую ``(max_chat_id, max_message_id)``
↔ ``(tg_chat_id, thread_id, tg_message_id)``. Без этого мост MAX→TG-реакций
не может зеркалить реакции на наши же сообщения (см. ``bridge::
_on_reaction_update`` — там логируется «DIALOG-mirror skip, no
DeliveredMessage»).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select, update

from shared.db._engine import session_scope
from shared.db._models import SendQueue


def enqueue_send(item: dict) -> int:
    """Положить задачу в очередь отправки. Возвращает ``id`` созданной записи.

    Дополнительно к стандартным полям (``text``, ``media_*``, ``thread_id``)
    можно передать:

    * ``tg_chat_id``    — id TG-супергруппы, из которой отправлено сообщение
      (топик которой = ``thread_id``). Нужен для создания ``DeliveredMessage``
      после успешной отправки в MAX.
    * ``tg_message_id`` — id TG-сообщения, из которого ушёл ответ
      (``message.message_id`` в aiogram).
    """
    with session_scope() as s:
        row = SendQueue(
            kind=item.get("kind", "text"),
            target_chat_id=item["target_chat_id"],
            text=item.get("text"),
            media_path=item.get("media_path"),
            media_mime=item.get("media_mime"),
            media_filename=item.get("media_filename"),
            created_by=item.get("created_by"),
            status="pending",
        )
        thread_id = item.get("thread_id")
        if thread_id is not None:
            row.thread_id = int(thread_id)
        tg_chat_id = item.get("tg_chat_id")
        if tg_chat_id is not None:
            row.tg_chat_id = int(tg_chat_id)
        tg_message_id = item.get("tg_message_id")
        if tg_message_id is not None:
            row.tg_message_id = int(tg_message_id)
        s.add(row)
        s.flush()
        return row.id


def claim_next_send() -> Optional[SendQueue]:
    """Атомарно берёт следующую задачу и помечает ``in_progress``."""
    with session_scope() as s:
        row = s.execute(
            select(SendQueue)
            .where(SendQueue.status == "pending")
            .order_by(SendQueue.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        if not row:
            return None
        row.status = "in_progress"
        s.flush()
        s.expunge(row)
        return row


def finish_send(item_id: int, ok: bool, error: Optional[str] = None) -> None:
    """Пометить задачу ``sent``/``failed`` после отправки в MAX."""
    with session_scope() as s:
        s.execute(
            update(SendQueue)
            .where(SendQueue.id == item_id)
            .values(
                status="sent" if ok else "failed",
                error=error,
                finished_at=datetime.utcnow(),
            )
        )


def queue_stats() -> dict:
    """Статистика по очереди для ``/status``: pending/in_progress/failed/sent."""
    with session_scope() as s:
        pending = s.query(SendQueue).filter(SendQueue.status == "pending").count()
        in_progress = s.query(SendQueue).filter(SendQueue.status == "in_progress").count()
        failed = s.query(SendQueue).filter(SendQueue.status == "failed").count()
        sent = s.query(SendQueue).filter(SendQueue.status == "sent").count()
        return {"pending": pending, "in_progress": in_progress, "failed": failed, "sent": sent}