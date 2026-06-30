"""Хэндлер ``MessageReactionUpdated`` → ``reaction_ops_queue`` (to_max).

Когда владелец моста ставит/снимает реакцию на сообщение бота в
Telegram-супергруппе (топике), aiogram присылает апдейт
``MessageReactionUpdated``. Здесь мы:

1. Фильтруем: реагировать может только ``owner_user_id`` из
   ``ALLOWED_TG_USER_IDS`` и только в нашей привязанной супергруппе.
2. По ``tg_message_id`` достаём ``max_chat_id`` / ``max_message_id``
   через ``DeliveredMessage.tg_message_id`` (заполняется в EventPoller).
3. Сравниваем ``old_reaction`` vs ``new_reaction``: добавленные →
   ``add``, удалённые → ``remove``.
4. Кладём задачи в ``reaction_ops_queue`` (direction=to_max); MAX-процесс
   заберёт их через ``/reaction_ops/next?direction=to_max`` и применит
   через ``client.add_reaction`` / ``client.remove_reaction``.

Сообщения в ЛС с ботом игнорируем: ``tg_chat_id == sg.supergroup_chat_id``
гарантирует, что это топик супергруппы (или её General).

Если сообщение из MAX ещё не доставлено (нет TG-ссылки) — пропускаем
(реакция в TG «уйдёт в воздух» с точки зрения моста; ничто не падает).
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Set

from aiogram import Bot, Router
from aiogram.types import (
    MessageReactionCountUpdated,
    MessageReactionUpdated,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    ReactionTypePaid,
)

from app.api_client import api
from app.config import settings
from shared import db as shared_db

logger = logging.getLogger(__name__)
router = Router(name="reactions-tg")


def _emoji_of(reaction: object) -> Optional[str]:
    """Достаём ``emoji`` строкой из ``ReactionTypeEmoji``.

    Другие типы (``custom_emoji``, ``paid``) пока не поддержаны MAX'ом
    на реакции — игнорируем (возвращаем ``None``).
    """
    if isinstance(reaction, ReactionTypeEmoji):
        return reaction.emoji
    if isinstance(reaction, ReactionTypeCustomEmoji):
        # MAX не поддерживает custom-emoji. Если TG прислал только
        # custom_emoji — не зеркалим.
        return None
    if isinstance(reaction, ReactionTypePaid):
        # Telegram Stars — MAX не умеет. Игнор.
        return None
    # Любой другой будущий тип — игнорируем.
    return None


def _diff(
    old: Iterable[object], new: Iterable[object]
) -> tuple[Set[str], Set[str]]:
    """Сравнить два списка реакций: вернуть (added, removed) emoji-строк.

    На входе — массивы ``ReactionType*``. Сравниваем только emoji-реакции.
    """
    old_emojis = {_emoji_of(r) for r in old}
    new_emojis = {_emoji_of(r) for r in new}
    old_emojis.discard(None)
    new_emojis.discard(None)
    added = new_emojis - old_emojis
    removed = old_emojis - new_emojis
    return added, removed


def _is_our_supergroup(chat_id: int) -> bool:
    """True, если ``chat_id`` совпадает с привязанной супергруппой владельца."""
    if not settings.allowed_tg_user_ids:
        return False
    owner_uid = settings.allowed_tg_user_ids[0]
    sg = shared_db.get_supergroup_for_owner(owner_uid)
    return bool(sg) and sg.supergroup_chat_id == chat_id


def _enqueue_op(
    op: str,
    max_chat_id: str,
    max_message_id: str,
    emoji: str,
) -> None:
    """Положить задачу в ``reaction_ops_queue`` через BotApi."""
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        coro = api.enqueue_reaction_op_to_max(
            op=op,
            max_chat_id=max_chat_id,
            max_message_id=max_message_id,
            emoji=emoji,
        )
        if loop.is_running():
            # Этот код вызывается из синхронного хэндлера, но мы
            # внутри aiogram (async) контекста — используем
            # ``asyncio.ensure_future``.
            asyncio.ensure_future(coro)
        else:
            loop.run_until_complete(coro)
    except Exception as exc:
        logger.warning(
            "reactions_tg: enqueue_reaction_op_to_max failed: %s", exc,
        )


async def _process_reaction_change(
    chat_id: int,
    message_id: int,
    user_id: int,
    added: Set[str],
    removed: Set[str],
) -> None:
    """Найти ``max_chat_id``/``max_message_id`` и поставить задачи в очередь."""
    if not added and not removed:
        return
    mapping = shared_db.get_delivered_by_tg_message(
        tg_chat_id=chat_id, tg_message_id=message_id,
    )
    if mapping is None:
        logger.debug(
            "reactions_tg: no DeliveredMessage for tg=%s/%s — skip",
            chat_id, message_id,
        )
        return
    max_chat_id = str(mapping.max_chat_id)
    max_message_id = str(mapping.max_message_id)
    for emoji in added:
        try:
            await api.enqueue_reaction_op_to_max(
                op="add",
                max_chat_id=max_chat_id,
                max_message_id=max_message_id,
                emoji=str(emoji),
            )
        except Exception as exc:
            logger.warning(
                "reactions_tg: enqueue add %s failed: %s", emoji, exc,
            )
    for emoji in removed:
        try:
            await api.enqueue_reaction_op_to_max(
                op="remove",
                max_chat_id=max_chat_id,
                max_message_id=max_message_id,
                emoji=str(emoji),
            )
        except Exception as exc:
            logger.warning(
                "reactions_tg: enqueue remove %s failed: %s", emoji, exc,
            )
    logger.info(
        "reactions_tg: user=%s msg=%s added=%s removed=%s → max=%s/%s",
        user_id, message_id, sorted(added), sorted(removed),
        max_chat_id, max_message_id,
    )


@router.message_reaction()
async def on_message_reaction_updated(
    event: MessageReactionUpdated, bot: Bot
) -> None:
    """Обработчик изменения реакций на конкретном сообщении.

    Регистрируется через ``router.message_reaction()`` в aiogram 3.x.
    """
    try:
        if event.chat is None or event.user is None:
            return
        chat_id = event.chat.id
        user_id = event.user.id
        message_id = event.message_id

        if not _is_our_supergroup(chat_id):
            return
        if not settings.allowed_tg_user_ids:
            return
        if user_id not in settings.allowed_tg_user_ids:
            # Реакции других пользователей не зеркалим (см. ограничения
            # в docs/features.md).
            return

        added, removed = _diff(event.old_reaction or [], event.new_reaction or [])
        await _process_reaction_change(chat_id, message_id, user_id, added, removed)
    except Exception as exc:
        logger.exception(
            "on_message_reaction_updated failed: %s", exc,
        )


@router.message_reaction_count()
async def on_message_reaction_count_updated(
    event: MessageReactionCountUpdated, bot: Bot
) -> None:
    """Обработчик изменения счётчика анонимных реакций.

    Приходит, когда кто-то реагирует на сообщение, но сам бот не знает,
    кто именно (например, для анонимных опросов). Нам этот апдейт не
    нужен — TG → MAX зеркалируем только владельца моста через
    ``MessageReactionUpdated``.
    """
    # No-op: документируем на будущее. Можно использовать для сбора
    # агрегированной статистики, если потребуется.
    return None