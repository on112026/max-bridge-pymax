"""edit_to_max.py — редактирование сообщения в TG-топике → MAX.

Когда пользователь редактирует уже отправленное сообщение в TG-топике
супергруппы, aiogram генерирует ``edited_message`` (не ``message``).
Этот хендлер:

1. Проверяет что сообщение из нашей supergroup и от владельца.
2. По ``tg_message_id`` находит ``DeliveredMessage`` (MAX chat_id + msg_id).
3. Ставит в очередь ``SendQueue(kind='edit', ...)`` через ``enqueue_edit``.
4. MAX-процесс (sender.py) заберёт задачу и вызовет ``client.edit_message``.

Ограничения:
- Редактировать можно только исходящие сообщения (отправленные нами в MAX).
- Входящие сообщения (переброшенные из MAX) редактировать нельзя
  (DeliveredMessage для них не сохраняется в outgoing).
"""

from __future__ import annotations

import logging

from aiogram import types

from app.api_client import api
from app.config import settings
from app.handlers._common import _is_allowed
from shared import db as shared_db

logger = logging.getLogger(__name__)


async def edited_message_to_max(message: types.Message) -> None:
    """Хендлер ``edited_message`` из TG-топика → редактирование в MAX."""
    if not message.from_user or not _is_allowed(message.from_user.id):
        return

    # Работаем только в форум-группе (supergroup с топиками)
    if not getattr(message.chat, "is_forum", False):
        return

    tg_chat_id = message.chat.id
    tg_message_id = message.message_id
    new_text = message.text or message.caption or ""

    if not new_text:
        logger.debug("edited_message_to_max: пустой текст, пропускаем")
        return

    # Ищем исходящее сообщение по tg_message_id — оно должно быть
    # в DeliveredMessage как исходящее (отправленное нами).
    # Используем get_delivered_by_tg_message.
    mapping = shared_db.get_delivered_by_tg_message(
        tg_chat_id=str(tg_chat_id),
        tg_message_id=str(tg_message_id),
    )
    if not mapping:
        logger.debug(
            "edited_message_to_max: нет DeliveredMessage для tg_chat=%s tg_msg=%s "
            "— не наше исходящее сообщение, пропускаем",
            tg_chat_id, tg_message_id,
        )
        return

    max_chat_id = mapping.max_chat_id
    max_message_id = mapping.max_message_id

    item_id = shared_db.enqueue_edit(
        target_chat_id=max_chat_id,
        target_max_message_id=max_message_id,
        new_text=new_text,
        created_by=message.from_user.id,
    )
    logger.info(
        "edited_message_to_max: queued edit id=%s chat=%s max_msg=%s text_len=%d",
        item_id, max_chat_id, max_message_id, len(new_text),
    )
