"""Логика отправки сообщений из Telegram в MAX через бота.

Два пути входа в «режим ответа»:

* ``/reply <chat_id>`` — пользователь явно говорит, в какой MAX-чат
  отправлять следующее сообщение. Бот переходит в ``ReplyState``.
* inline-кнопка «💬 Ответить» под сообщением из MAX — ``chat_id``
  подтягивается из БД через ``api.get_event``.

После перехода в FSM:

* ``reply_text`` ловит текст → ``api.enqueue_send(kind="text")``.
* ``reply_media`` ловит фото/видео/документ → скачивает файл из TG в
  ``outbox/``, кладёт в очередь ``kind="photo"|"video"|"document"``.

После любой отправки:

* Чат помечается прочитанным (``mark_chat_read_up_to``), чтобы
  MAX-процесс синхронизировал ``client.read_message``.

``/cancel`` сбрасывает любое FSM-состояние (``reply``, ``upload_session``,
``reauth_sms``).
"""

from __future__ import annotations

import logging
import os

from aiogram import types
from aiogram.fsm.context import FSMContext

from app.api_client import api
from app.config import settings
from app.handlers._common import (
    MAX_TG_DOWNLOAD,
    _escape,
    _is_allowed,
    _reject,
)
from app.states import ReplyState

logger = logging.getLogger(__name__)


# ---------- /cancel — единый сброс FSM ----------


async def cancel_command(message: types.Message, state: FSMContext) -> None:
    """``/cancel`` — выйти из любого FSM-состояния (reply / reauth / upload)."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    await state.clear()
    await message.answer("Ок, вышел из режима.")


# ---------- /reply <chat_id> ----------


async def reply_command(message: types.Message, state: FSMContext) -> None:
    """``/reply <chat_id>`` — следующее сообщение уйдёт в этот MAX-чат.

    Если пользователь вызвал команду **внутри топика** supergroup —
    запоминаем ``message_thread_id``, чтобы ответ ушёл в тот же топик
    (полезно, чтобы собеседник в MAX понимал, откуда пришёл ответ).
    """
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Использование: /reply <chat_id>")
        return
    # Если пользователь вызвал /reply внутри топика supergroup — запоминаем
    # message_thread_id, чтобы ответ ушёл в тот же топик.
    thread_id = (
        message.message_thread_id
        if getattr(message, "is_topic_message", False)
        else None
    )
    await state.set_state(ReplyState.waiting_text)
    await state.update_data(
        target_chat_id=args[1],
        thread_id=thread_id,
    )
    if thread_id:
        await message.answer(
            f"✍️ Введите сообщение для чата <code>{_escape(args[1])}</code> "
            "(в текущий топик).\n"
            "Можно отправить фото/видео/документ.\n"
            "/cancel — выйти.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"✍️ Введите сообщение для чата <code>{_escape(args[1])}</code>.\n"
            "Можно отправить фото/видео/документ — всё уйдёт туда.\n"
            "/cancel — выйти.",
            parse_mode="HTML",
        )


# ---------- FSM-обработчики: text и media ----------


async def reply_text(message: types.Message, state: FSMContext) -> None:
    """FSM-обработчик текста в состоянии ``ReplyState.waiting_text``."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    data = await state.get_data()
    target = data.get("target_chat_id")
    if not target:
        await state.clear()
        return
    text = message.text or ""
    thread_id = data.get("thread_id")
    try:
        res = await api.enqueue_send(
            target_chat_id=target,
            kind="text",
            text=text,
            created_by=message.from_user.id,
            thread_id=thread_id,
        )
        await message.answer(
            f"✅ Отправлено в очередь (id={res.get('id')}). "
            "Дождитесь подтверждения от MAX."
            + (f"\n🧵 Ответ уйдёт в топик {thread_id}." if thread_id else "")
        )
    except Exception as exc:
        await message.answer(f"⚠️ Ошибка постановки в очередь: {exc}")
    # Помечаем чат как прочитанный (пользователь явно ответил).
    try:
        await api.mark_chat_read_up_to(chat_id=target)
    except Exception as exc:
        logger.warning("mark_chat_read_up_to failed: %s", exc)
    await state.clear()


async def reply_media(message: types.Message, state: FSMContext) -> None:
    """FSM-обработчик медиа (photo / video / document) в ``ReplyState.waiting_text``.

    Скачивает файл из TG в ``outbox/`` и кладёт запись в очередь отправки.
    Поддерживает только одно вложение за раз (PyMax ``send_message``
    принимает список, но мы держим единый интерфейс с TG send_photo/send_video).
    """
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    data = await state.get_data()
    target = data.get("target_chat_id")
    if not target:
        await state.clear()
        return

    kind = "document"
    file_id = None
    caption = message.caption or ""
    if message.photo:
        kind = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        kind = "video"
        file_id = message.video.file_id
    elif message.document:
        kind = "document"
        file_id = message.document.file_id
    else:
        await message.answer("Пришлите текст, фото, видео или документ.")
        return

    if not file_id:
        await message.answer("Не удалось получить файл.")
        return

    thread_id = data.get("thread_id")
    try:
        tg_file = await message.bot.get_file(file_id)
        if tg_file.file_size and tg_file.file_size > MAX_TG_DOWNLOAD:
            await message.answer("Файл больше 50 МБ — Telegram не отдаёт его боту.")
            return
        os.makedirs(os.path.join(settings.media_dir, "outbox"), exist_ok=True)
        local_name = f"{tg_file.file_unique_id}_{os.path.basename(tg_file.file_path or 'file')}"
        local_path = os.path.join(settings.media_dir, "outbox", local_name)
        await message.bot.download_file(tg_file.file_path, local_path)
        rel = os.path.relpath(local_path, settings.media_dir)
        res = await api.enqueue_send(
            target_chat_id=target,
            kind=kind,
            text=caption,
            media_path=rel,
            media_mime=message.content_type,
            media_filename=local_name,
            created_by=message.from_user.id,
            thread_id=thread_id,
        )
        await message.answer(
            f"📨 Медиа поставлено в очередь (id={res.get('id')}, {kind}). "
            "Дождитесь подтверждения от MAX."
            + (f"\n🧵 Ответ уйдёт в топик {thread_id}." if thread_id else "")
        )
    except Exception as exc:
        await message.answer(f"⚠️ Ошибка: {exc}")
    # Помечаем чат как прочитанный.
    try:
        await api.mark_chat_read_up_to(chat_id=target)
    except Exception as exc:
        logger.warning("mark_chat_read_up_to failed: %s", exc)
    await state.clear()