"""«Эхо» из топика супергруппы в MAX без команды ``/reply``.

Когда владелец пишет сообщение в топике привязанной Telegram-супергруппы,
бот автоматически находит соответствующий MAX-чат (по ``chat_topic``
в БД) и кладёт сообщение в очередь отправки в MAX.

Логика:

0. Если идёт FSM-режим ответа (``ReplyState``) — пропускаем.
1. Авторизация: только владелец бота.
2. Не зацикливать свои же пересылки из MAX (автор — бот).
3. Должно быть сообщение именно в топике супергруппы (``is_forum``).
4. Фильтр по нашей супергруппе (по ``supergroup_chat_id`` владельца).
5. Только поддерживаемые типы контента (``text``/``photo``/``video``/
   ``document``/``voice``). Команды (``/...``) пропускаем.
6. По ``(chat.id, thread_id)`` ищем ``ChatTopic`` в БД → ``max_chat_id``.
7. Скачиваем медиа в ``outbox/`` и кладём запись в очередь.
8. Помечаем чат прочитанным в MAX (``mark_chat_read_up_to``).
9. Ставим ✅-реакцию на исходное сообщение как подтверждение.

Регистрируется в ``registration.py`` **последним** с фильтром
``F.func(lambda m: bool(getattr(getattr(m, "chat", None), "is_forum", False)))``,
чтобы срабатывал только когда не сматчились ни команда, ни FSM-состояние.
"""

from __future__ import annotations

import logging
import os

from aiogram import Bot, types
from aiogram.fsm.context import FSMContext

from app.api_client import api
from app.config import settings
from app.handlers._common import (
    MAX_TG_DOWNLOAD,
    _is_allowed,
)
from app.states import ReplyState
from shared import db as shared_db

logger = logging.getLogger(__name__)


async def _echo_react(bot: Bot, chat_id: int, message_id: int, emoji: str = "✅") -> None:
    """Ставит emoji-реакцию на сообщение в топике (тихое подтверждение).

    В Bot API 7.0+ (aiogram 3) есть ``setMessageReaction``. Если метод
    недоступен или бот не админ с правом reactions — просто ничего не делаем,
    не отправляя текстовое «Отправлено» в топик (чтобы не мусорить).
    """
    try:
        from aiogram.methods import SetMessageReaction
        await bot(SetMessageReaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[{"type": "emoji", "emoji": emoji}],
        ))
    except Exception as exc:
        logger.debug("set_reaction failed for %s/%s: %s", chat_id, message_id, exc)


async def topic_message_to_max(
    message: types.Message, state: FSMContext, bot: Bot
) -> None:
    """«Эхо» из топика супергруппы в MAX без команды /reply."""
    # 0) Если идёт FSM-режим ответа — не перехватываем сообщение.
    cur = await state.get_state()
    if cur == ReplyState.waiting_text:
        return

    # 1) Авторизация: только владелец бота.
    if not message.from_user or not _is_allowed(message.from_user.id):
        return
    # 2) Не зацикливать свои же пересылки из MAX.
    if message.from_user.is_bot:
        return

    # 3) Должно быть сообщение именно в топике супергруппы.
    thread_id = getattr(message, "message_thread_id", None)
    if not thread_id:
        return  # обычное сообщение в General / в личке бота
    chat = message.chat
    if not getattr(chat, "is_forum", False):
        return

    # 4) Фильтр по нашей супергруппе. ``supergroup_chat_id`` уже
    #    запомнен через /setgroup; берём по первому владельцу из
    #    allowed_tg_user_ids (у нас он один).
    owner_uid = (
        settings.allowed_tg_user_ids[0]
        if settings.allowed_tg_user_ids else 0
    )
    sg = shared_db.get_supergroup_for_owner(owner_uid) if owner_uid else None
    if not sg or sg.supergroup_chat_id != chat.id:
        # Не наша группа (бот может состоять и в чужих группах) — игнор.
        return

    # 5) Только поддерживаемые типы контента. Сервисные сообщения
    #    (forum topic edited, member joined и т.п.) сюда не попадут.
    if message.content_type not in {"text", "photo", "video", "document", "voice"}:
        return
    # Команды — aiogram и так их не доведёт до этого хэндлера, но на всякий
    # случай (вдруг фильтр в регистрации ослабнет) — игнорируем текст-слеш.
    if message.content_type == "text" and (message.text or "").startswith("/"):
        return

    # 6) По (chat.id, thread_id) → ChatTopic → max_chat_id.
    topic = shared_db.get_topic_by_thread_id(chat.id, int(thread_id))
    if not topic:
        logger.info(
            "topic_echo: no ChatTopic for chat=%s thread=%s — drop message",
            chat.id, thread_id,
        )
        return
    max_chat_id = topic.max_chat_id

    # 7) Отправляем в очередь (идём по тому же пути, что и /reply).
    # ``tg_chat_id`` / ``tg_message_id`` — id TG-сообщения, из которого
    # уходит ответ. MAX-процесс после ``client.send_message`` создаст
    # ``DeliveredMessage``-строку, связывающую ``(max_chat_id, msg.id)``
    # ↔ ``(chat.id, thread_id, message.message_id)``. Без этого мост
    # MAX→TG-реакций не сможет найти наше TG-сообщение по ``max_message_id``
    # (логирует «DIALOG-mirror skip, no DeliveredMessage»).
    try:
        if message.content_type == "text":
            res = await api.enqueue_send(
                target_chat_id=max_chat_id,
                kind="text",
                text=message.text or "",
                created_by=message.from_user.id,
                thread_id=int(thread_id),
                tg_chat_id=int(chat.id),
                tg_message_id=int(message.message_id),
            )
        else:
            file_id = None
            kind = "document"
            if message.photo:
                kind, file_id = "photo", message.photo[-1].file_id
            elif message.video:
                kind, file_id = "video", message.video.file_id
            elif message.document:
                kind, file_id = "document", message.document.file_id
            elif message.voice:
                kind, file_id = "voice", message.voice.file_id
            if not file_id:
                return

            tg_file = await bot.get_file(file_id)
            if tg_file.file_size and tg_file.file_size > MAX_TG_DOWNLOAD:
                await message.reply(
                    "Файл больше 50 МБ — Telegram не отдаёт его боту. "
                    "Отправка в MAX отменена."
                )
                return
            os.makedirs(
                os.path.join(settings.media_dir, "outbox"), exist_ok=True
            )
            local_name = (
                f"{tg_file.file_unique_id}_"
                f"{os.path.basename(tg_file.file_path or 'file')}"
            )
            local_path = os.path.join(settings.media_dir, "outbox", local_name)
            await bot.download_file(tg_file.file_path, local_path)
            rel = os.path.relpath(local_path, settings.media_dir)
            res = await api.enqueue_send(
                target_chat_id=max_chat_id,
                kind=kind,
                text=message.caption or "",
                media_path=rel,
                media_mime=message.content_type,
                media_filename=local_name,
                created_by=message.from_user.id,
                thread_id=int(thread_id),
                tg_chat_id=int(chat.id),
                tg_message_id=int(message.message_id),
            )
        logger.info(
            "topic_echo: enqueued send id=%s chat=%s thread=%s from uid=%s "
            "tg_chat_id=%s tg_message_id=%s",
            res.get("id") if isinstance(res, dict) else res,
            max_chat_id, thread_id, message.from_user.id,
            chat.id, message.message_id,
        )
    except Exception as exc:
        logger.warning(
            "topic_echo: enqueue_send failed chat=%s: %s", max_chat_id, exc,
        )
        try:
            await message.reply(
                f"⚠️ Не удалось отправить в MAX: {exc}"
            )
        except Exception:
            pass
        return

    # 8) Помечаем чат как прочитанный (как делают /reply и inline-кнопки).
    try:
        await api.mark_chat_read_up_to(chat_id=max_chat_id)
    except Exception as exc:
        logger.debug("topic_echo: mark_chat_read_up_to failed: %s", exc)

    # 9) Тихое подтверждение реакцией ✅.
    await _echo_react(bot, chat.id, message.message_id)