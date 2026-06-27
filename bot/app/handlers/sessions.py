"""Загрузка и управление session-файлами MAX.

PyMax сохраняет сессию в файл (по умолчанию ``bridge.db`` в ``CACHE_DIR``).
Владелец может:

* залить session-файл через бота (reply-кнопка «📂 Загрузить сессию MAX»
  или команда ``/upload_session`` → ``POST /admin/session/upload``);
* вызвать ``/sessions`` — посмотреть список доступных session-файлов
  в кэше и быстро выбрать нужный через inline-кнопки;
* нажать «Использовать эту сессию» под сообщением ``/sessions`` —
  ``POST /admin/session/use`` копирует выбранный файл в ``bridge.db``.

После загрузки/выбора AuthWatcher (отдельный модуль) ловит изменение
``auth_state.session_file_path`` и ``status=session_attached`` →
присылает inline-меню «📂 Подключиться по сессии» (это уже логика
``auth.py``, не здесь).
"""

from __future__ import annotations

import io
import logging
from datetime import datetime

from aiogram import types
from aiogram.fsm.context import FSMContext

from app.api_client import api
from app.handlers._common import (
    MAX_SESSION_SIZE,
    _is_allowed,
    _reject,
)
from app.keyboards import session_use_keyboard
from app.states import UploadSessionState

logger = logging.getLogger(__name__)


# ---------- /upload_session — вход в FSM загрузки session-файла ----------


async def button_upload_session(message: types.Message, state: FSMContext) -> None:
    """Reply-кнопка «📂 Загрузить сессию MAX» → запрос файла."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    await state.set_state(UploadSessionState.waiting_file)
    await message.answer(
        "📂 Пришлите <b>документом</b> файл сессии MAX "
        "(обычно <code>bridge.db</code>, до 50 МБ).\n"
        "После загрузки файл попадёт в кэш MAX, и я пришлю inline-меню "
        "«📂 Подключиться по сессии».\n"
        "/cancel — отмена.",
        parse_mode="HTML",
    )


async def upload_session_command(message: types.Message, state: FSMContext) -> None:
    """Команда ``/upload_session`` — то же, что и reply-кнопка."""
    await button_upload_session(message, state)


# ---------- FSM-обработчик документа ----------


async def upload_session_file_handler(message: types.Message, state: FSMContext) -> None:
    """FSM-обработчик документа в состоянии ``UploadSessionState.waiting_file``.

    Скачивает файл из Telegram, отдаёт в ``/admin/session/upload``,
    сбрасывает FSM и подсказывает дальнейшее действие («📂 Подключиться
    по сессии» — inline-кнопка из AuthWatcher'а придёт сама).
    """
    if not _is_allowed(message.from_user.id):
        await state.clear()
        return await _reject(message)

    doc = message.document
    if not doc:
        await message.answer(
            "Жду файл сессии <b>документом</b> (не картинкой). "
            "/cancel — отмена.",
            parse_mode="HTML",
        )
        return

    # Проверим размер до скачивания (Telegram всё равно отдаёт file_size).
    if doc.file_size and doc.file_size > MAX_SESSION_SIZE:
        await message.answer("Файл больше 50 МБ — не подходит.")
        return

    try:
        tg_file = await message.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await message.bot.download_file(tg_file.file_path, buf)
        data = buf.getvalue()
    except Exception as exc:
        logger.warning("download session file failed: %s", exc)
        await message.answer(f"⚠️ Не удалось скачать файл: {exc}")
        return

    if not data:
        await message.answer("⚠️ Файл пустой.")
        return

    # Сообщим владельцу, что файл ушёл в api.
    filename = doc.file_name or "bridge.db"
    await message.answer(
        f"📤 Загружаю {filename} ({len(data)} байт) на сервер…"
    )

    try:
        result = await api.upload_session_file(
            file_bytes=data,
            filename=filename,
            content_type=doc.mime_type or "application/octet-stream",
        )
    except Exception as exc:
        logger.warning("upload_session_file failed: %s", exc)
        await message.answer(f"⚠️ API отказал в загрузке: {exc}")
        return

    logger.info(
        "session uploaded by uid=%s name=%s size=%d path=%s",
        message.from_user.id, filename, len(data),
        result.get("path") if isinstance(result, dict) else "?",
    )
    await message.answer(
        f"✅ Session-файл принят ({len(data)} байт).\n"
        "Подождите ~3 секунды — пришлю inline-меню с кнопкой «📂 Подключиться по сессии»."
    )
    await state.clear()


# ---------- /sessions — список доступных session-файлов ----------


async def sessions_command(message: types.Message) -> None:
    """``/sessions`` — список доступных session-файлов в кэше.

    Возвращает текстовый список + inline-кнопки для быстрого выбора
    (callback'и ``SessionUseCallback`` и ``sessions_refresh``).
    """
    if not _is_allowed(message.from_user.id):
        return await _reject(message)

    await message.answer("🔄 Запрашиваю список сессий...")

    try:
        data = await api.get_session_list()

        if not data or not data.get("sessions"):
            await message.answer("📭 В кэше нет сохранённых session-файлов.")
            return

        sessions = data["sessions"]
        current = data.get("current")

        text = "📂 **Доступные session-файлы:**\n\n"
        for s in sessions[:15]:  # лимит для читаемости
            size_kb = s["size"] // 1024
            mod_time = ""
            if s.get("modified"):
                dt = datetime.fromtimestamp(s["modified"])
                mod_time = f" • {dt.strftime('%d.%m %H:%M')}"

            current_mark = " ✅ **ТЕКУЩАЯ**" if current and s["path"] == current else ""
            text += f"• `{s['name']}` ({size_kb} КБ){mod_time}{current_mark}\n"

        text += f"\n**Всего:** {len(sessions)} файл(ов)"

        # Кнопки для быстрого выбора
        kb = session_use_keyboard([s["name"] for s in sessions[:8]])

        await message.answer(text, parse_mode="Markdown", reply_markup=kb)

    except Exception as exc:
        logger.error("sessions_command failed: %s", exc)
        await message.answer(f"❌ Не удалось получить список сессий: {exc}")


async def session_use_callback(callback: types.CallbackQuery) -> None:
    """Inline-кнопка «Использовать эту сессию» — копирует файл в bridge.db.

    callback_data формата ``session_use:<name>``. После копирования
    ``auth_state.session_file_path`` обновится, AuthWatcher увидит и
    пришлёт inline-меню «📂 Подключиться по сессии».
    """
    if not callback.from_user or not _is_allowed(callback.from_user.id):
        return await callback.answer("⛔", show_alert=True)

    if not callback.data or ":" not in callback.data:
        return await callback.answer()

    _, session_name = callback.data.split(":", 1)

    await callback.answer(f"Используем {session_name}...")

    try:
        await api.use_session(session_name)
        await callback.message.answer(
            f"✅ Сессия `{session_name}` скопирована в `bridge.db`.\n"
            "Теперь нажмите «📂 Подключиться по сессии» в меню авторизации."
        )
    except Exception as exc:
        logger.warning("session_use failed for %s: %s", session_name, exc)
        await callback.message.answer(f"❌ Не удалось использовать сессию: {exc}")


async def sessions_refresh_callback(callback: types.CallbackQuery) -> None:
    """Inline-кнопка «🔄 Обновить список» под ``/sessions``.

    Перезапрашивает список session-файлов и редактирует текущее сообщение.
    Если редактирование невозможно (например, нет текста) — шлём новое.
    """
    if not callback.from_user or not _is_allowed(callback.from_user.id):
        return await callback.answer("⛔", show_alert=True)

    await callback.answer("🔄 Обновляю...")

    try:
        data = await api.get_session_list()
    except Exception as exc:
        logger.warning("sessions_refresh failed: %s", exc)
        await callback.message.answer(f"❌ Не удалось получить список сессий: {exc}")
        return

    if not data or not data.get("sessions"):
        await callback.message.answer("📭 В кэше нет сохранённых session-файлов.")
        return

    sessions = data["sessions"]
    current = data.get("current")

    text = "📂 **Доступные session-файлы:**\n\n"
    for s in sessions[:15]:
        size_kb = s["size"] // 1024
        mod_time = ""
        if s.get("modified"):
            dt = datetime.fromtimestamp(s["modified"])
            mod_time = f" • {dt.strftime('%d.%m %H:%M')}"

        current_mark = " ✅ **ТЕКУЩАЯ**" if current and s["path"] == current else ""
        text += f"• `{s['name']}` ({size_kb} КБ){mod_time}{current_mark}\n"

    text += f"\n**Всего:** {len(sessions)} файл(ов)"

    kb = session_use_keyboard([s["name"] for s in sessions[:8]])
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        # Если сообщение нельзя редактировать (например, нет текста) — шлём новое.
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)


async def button_sessions(message: types.Message) -> None:
    """Reply-кнопка «📋 Сессии» → ``sessions_command``."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    await sessions_command(message)