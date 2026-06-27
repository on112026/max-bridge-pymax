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
    CloseForumTopic,
    CreateForumTopic,
    EditForumTopic,
    ExportChatInviteLink,
    GetChat,
)

from shared import db as shared_db

logger = logging.getLogger(__name__)


# Telegram Bot API лимит на длину имени топика — 128 символов.
TOPIC_NAME_MAX_LEN = 128


# Маппинг типа чата из pymax (``pymax.types.domain.enums.ChatType``) на
# короткую русскую подпись для имени топика. В pymax есть только
# DIALOG/CHAT/CHANNEL — отдельного «бот» в библиотеке нет, поэтому
# личные диалоги (включая чаты с ботами MAX) всегда отрисовываются
# как «ЛС». Если MAX пришлёт неизвестное значение — фолбэк на «MAX».
_CHAT_TYPE_LABEL = {
    "DIALOG": "ЛС",
    "CHAT": "группа",
    "CHANNEL": "канал",
}


def _format_type_label(chat_type: Optional[str]) -> Optional[str]:
    """Вернуть русскую подпись для типа чата или ``None`` если неизвестен.

    ``None``/пустая строка → ``None`` (используется как сигнал «оставить
    старый формат ``(MAX: <id>)`` для обратной совместимости и для
    чатов без известного типа).
    """
    if not chat_type:
        return None
    return _CHAT_TYPE_LABEL.get(str(chat_type).strip().upper())


def _make_topic_display_name(
    chat_title: str,
    max_chat_id: str,
    chat_type: Optional[str] = None,
) -> str:
    """Собирает имя топика.

    Формат зависит от ``chat_type``:

    * ``DIALOG`` → ``"<title> (ЛС: <id>)"`` (или ``"(ЛС: <id>)"`` если
      имя пустое);
    * ``CHAT``   → ``"<title> (группа: <id>)"``;
    * ``CHANNEL``→ ``"<title> (канал: <id>)"``;
    * неизвестный тип или ``None`` → старый формат ``(MAX: <id>)``.

    Лимит Telegram Bot API — 128 символов.
    """
    title = (chat_title or "").strip()
    label = _format_type_label(chat_type)
    if label:
        prefix = f"({label}: {max_chat_id})"
    else:
        prefix = f"(MAX: {max_chat_id})"
    if title:
        name = f"{title} {prefix}"
    else:
        name = prefix
    return name[:TOPIC_NAME_MAX_LEN]


async def get_or_create_topic(
    bot: Bot,
    supergroup_chat_id: int,
    max_chat_id: str,
    chat_title: Optional[str] = None,
    chat_type: Optional[str] = None,
) -> Optional[int]:
    """Возвращает ``message_thread_id`` для ``max_chat_id``.

    Если топик ещё не создан — создаёт его в указанной supergroup
    с именем ``chat_title (<label>: <id>)`` (``label`` — русская подпись
    типа чата: «ЛС» / «группа» / «канал»). Если ``chat_type`` неизвестен
    или не передан — имя строится в старом формате ``(MAX: <id>)``.

    ``chat_type`` — тип чата из pymax (``DIALOG`` / ``CHAT`` / ``CHANNEL``),
    пробрасывается из payload ``/internal/sync_topics`` и из таблицы ``chats``.

    Возвращает ``None`` при ошибке (например, бот не админ в группе).
    """
    max_chat_id = str(max_chat_id)
    existing = shared_db.get_topic(max_chat_id)
    if existing:
        # Если имя в MAX поменялось или поменялся тип чата (chat_type
        # известен и его метка отличается от текущей в имени) —
        # переименовываем топик.
        desired_name = (chat_title or existing.topic_name or "").strip() or max_chat_id
        new_display_name = _make_topic_display_name(
            desired_name, max_chat_id, chat_type=chat_type
        )
        current_display_name = _make_topic_display_name(
            existing.topic_name or "", max_chat_id, chat_type=chat_type,
        )
        if (
            new_display_name != current_display_name
            and new_display_name != (existing.topic_name or "")
        ):
            try:
                await bot(EditForumTopic(
                    chat_id=supergroup_chat_id,
                    message_thread_id=existing.thread_id,
                    name=new_display_name,
                ))
                shared_db.update_topic_name(max_chat_id, desired_name)
                logger.info(
                    "renamed topic for %s → %r (type=%s)",
                    max_chat_id, desired_name, chat_type,
                )
            except (TelegramAPIError, TelegramRetryAfter) as exc:
                logger.warning(
                    "edit_forum_topic for %s failed: %s", max_chat_id, exc
                )
        return existing.thread_id

    display_name = _make_topic_display_name(
        chat_title or "", max_chat_id, chat_type=chat_type,
    )
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
        "created topic for %s: thread_id=%s name=%r (type=%s)",
        max_chat_id, topic.message_thread_id, display_name, chat_type,
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


async def rename_topic(
    bot: Bot,
    supergroup_chat_id: int,
    thread_id: int,
    max_chat_id: str,
    new_chat_title: Optional[str],
    chat_type: Optional[str] = None,
) -> bool:
    """Переименовать существующий топик и обновить ``ChatTopic.topic_name``.

    Используется из ``TopicSyncWorker`` при ``action="rename"``.

    ``chat_type`` — опциональный тип чата из MAX (``DIALOG`` / ``CHAT`` /
    ``CHANNEL``). Если передан — в имени топика будет «ЛС»/«группа»/«канал»
    вместо безликого «MAX». При ``None`` сохраняется старый формат.

    Возвращает ``True`` при успехе. На любой Telegram-ошибке возвращает
    ``False`` и не обновляет БД, чтобы воркер мог пометить джоб failed.
    """
    new_title = (new_chat_title or "").strip()
    display_name = _make_topic_display_name(
        new_title, max_chat_id, chat_type=chat_type,
    )
    try:
        await bot(EditForumTopic(
            chat_id=supergroup_chat_id,
            message_thread_id=thread_id,
            name=display_name,
        ))
    except (TelegramAPIError, TelegramRetryAfter) as exc:
        logger.warning(
            "rename_topic for %s thread=%s failed: %s",
            max_chat_id, thread_id, exc,
        )
        return False
    shared_db.update_topic_name(max_chat_id, new_title)
    logger.info(
        "renamed topic for %s → %r (thread=%s, type=%s)",
        max_chat_id, new_title, thread_id, chat_type,
    )
    return True


async def close_topic(
    bot: Bot,
    supergroup_chat_id: int,
    thread_id: int,
) -> bool:
    """Закрыть топик в Telegram через ``closeForumTopic``.

    Используется из ``/prune_topics``. Сам по себе метод только
    закрывает топик (но не удаляет — Telegram Bot API этого не умеет;
    пользователь может потом переоткрыть вручную). Пометку ``stale=2``
    в БД делает вызывающий код через ``api.close_stale_topic``.
    """
    try:
        await bot(CloseForumTopic(
            chat_id=supergroup_chat_id,
            message_thread_id=thread_id,
        ))
    except (TelegramAPIError, TelegramRetryAfter) as exc:
        logger.warning(
            "close_topic for thread=%s failed: %s",
            thread_id, exc,
        )
        return False
    logger.info("closed topic thread=%s in supergroup=%s", thread_id, supergroup_chat_id)
    return True
