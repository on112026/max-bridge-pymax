"""Работа с Telegram-топиками: lookup / create / rename для MAX-чатов.

Используется ``forwarder.py`` для каждого входящего события из MAX
и ``handlers.py`` для команды ``/setup`` / ``/getlink``.
"""

from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.methods import (
    CreateForumTopic,
    EditForumTopic,
    ExportChatInviteLink,
    GetChat,
)

from shared import db as shared_db

logger = logging.getLogger(__name__)


# Telegram Bot API лимит на длину имени топика — 128 символов.
TOPIC_NAME_MAX_LEN = 128


def _make_topic_display_name(chat_title: str, max_chat_id: str) -> str:
    """Собирает имя топика формата ``chat_title (MAX: <id>)``.

    Если ``chat_title`` пустое — используем только ``(MAX: <id>)``.
    """
    title = (chat_title or "").strip()
    if title:
        name = f"{title} (MAX: {max_chat_id})"
    else:
        name = f"(MAX: {max_chat_id})"
    return name[:TOPIC_NAME_MAX_LEN]


async def get_or_create_topic(
    bot: Bot,
    supergroup_chat_id: int,
    max_chat_id: str,
    chat_title: Optional[str] = None,
) -> Optional[int]:
    """Возвращает ``message_thread_id`` для ``max_chat_id``.

    Если топик ещё не создан — создаёт его в указанной supergroup
    с именем ``chat_title (MAX: <id>)``. Возвращает ``None`` при ошибке
    (например, бот не админ в группе).
    """
    max_chat_id = str(max_chat_id)
    existing = shared_db.get_topic(max_chat_id)
    if existing:
        # Если имя в MAX поменялось — переименовываем топик.
        desired_name = (chat_title or existing.topic_name or "").strip() or max_chat_id
        if desired_name != existing.topic_name:
            try:
                await bot(EditForumTopic(
                    chat_id=supergroup_chat_id,
                    message_thread_id=existing.thread_id,
                    name=_make_topic_display_name(desired_name, max_chat_id),
                ))
                shared_db.update_topic_name(max_chat_id, desired_name)
                logger.info(
                    "renamed topic for %s → %r", max_chat_id, desired_name
                )
            except (TelegramAPIError, TelegramRetryAfter) as exc:
                logger.warning(
                    "edit_forum_topic for %s failed: %s", max_chat_id, exc
                )
        return existing.thread_id

    display_name = _make_topic_display_name(chat_title or "", max_chat_id)
    try:
        topic = await bot(CreateForumTopic(
            chat_id=supergroup_chat_id,
            name=display_name,
        ))
    except (TelegramAPIError, TelegramRetryAfter) as exc:
        logger.warning(
            "create_forum_topic for %s failed: %s", max_chat_id, exc
        )
        return None

    shared_db.create_topic(
        max_chat_id=max_chat_id,
        supergroup_chat_id=supergroup_chat_id,
        thread_id=topic.message_thread_id,
        topic_name=(chat_title or "").strip() or None,
    )
    logger.info(
        "created topic for %s: thread_id=%s name=%r",
        max_chat_id, topic.message_thread_id, display_name,
    )
    return topic.message_thread_id


async def ensure_forum_enabled(bot: Bot, supergroup_chat_id: int) -> bool:
    """Проверяет, включён ли forum mode в группе.

    Возвращает ``True`` если уже включён. Если нет — ``False``;
    пользователю будет предложено включить топики вручную
    (см. ``handlers.setgroup_command``). Программно включить не пытаемся:
    метод ``setChatIsForum`` (Bot API 7.0+) в публичном ``aiogram.methods``
    этой сборки отсутствует, поэтому опираемся только на состояние ``is_forum``.
    """
    try:
        chat = await bot(GetChat(chat_id=supergroup_chat_id))
        return bool(getattr(chat, "is_forum", False))
    except (TelegramAPIError, TelegramRetryAfter) as exc:
        logger.warning(
            "ensure_forum_enabled: get_chat for %s failed: %s",
            supergroup_chat_id, exc,
        )
        return False


async def export_invite_link(bot: Bot, supergroup_chat_id: int) -> Optional[str]:
    """Получить (или пересоздать) ``invite_link`` для приватной группы."""
    try:
        return await bot(ExportChatInviteLink(chat_id=supergroup_chat_id))
    except (TelegramAPIError, TelegramRetryAfter) as exc:
        logger.warning(
            "export_chat_invite_link for %s failed: %s",
            supergroup_chat_id, exc,
        )
        return None