"""Хэндлеры команд и callback'ов Telegram-бота (этап 2, PyMax).

Модель авторизации «только по команде» (см. ``api/main.py``):

* На cold-start ``auth_state.status = auth_required`` и supervisor НЕ
  поднимает PyMax Client. Бот (через ``AuthWatcher``) видит это и присылает
  владельцу inline-меню: «🔐 SMS-авторизация», «📂 Подключиться по сессии»,
  «📎 Загрузить файл сессии», «⛔ Отмена».
* При нажатии inline-кнопки бот кладёт ``pending_action`` через
  ``/auth/action``, и supervisor на следующей итерации поднимает Client
  (с wipe cache для SMS, без wipe для session).
* Если владелец вручную положил session-файл в кэш (например, руками на
  сервере), supervisor увидит его через session-watcher и переведёт
  ``auth_state.status = session_attached``. Бот пришлёт короткое уведомление
  с inline-кнопкой «Подключиться по сессии».
* На каждом шаге бот присылает владельцу отдельное сообщение: «🔐 MAX
  не подключён…», «📨 Запрашиваю SMS у MAX…», «🔌 Пробую подключиться
  по сессии…», «⚠️ Не удалось…» — никаких «тихих» действий.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from app.api_client import api
from app.config import settings
from app.keyboards import (
    AuthActionCallback,
    auth_choice_keyboard,
    event_inline_keyboard,
    main_reply_keyboard,
)
from app.sender import forward_event
from app.states import ReplyState, ReauthSmsState, UploadSessionState
from app.keyboards import session_use_keyboard

logger = logging.getLogger(__name__)

MAX_TG_DOWNLOAD = 49 * 1024 * 1024
MAX_SESSION_SIZE = 50 * 1024 * 1024


def _is_allowed(user_id: int) -> bool:
    if not settings.allowed_tg_user_ids:
        return False
    return user_id in settings.allowed_tg_user_ids


async def _reject(message: types.Message) -> None:
    await message.answer("⛔ Бот принимает сообщения только от авторизованных пользователей.")


def _escape(text: str) -> str:
    return (text or "").replace("&", "&").replace("<", "<").replace(">", ">")


def _format_chat(chat: Dict[str, Any]) -> str:
    title = chat.get("title") or "—"
    cid = chat.get("max_chat_id")
    last = chat.get("last_message_preview") or ""
    return f"<b>{_escape(title)}</b>\nID: <code>{cid}</code>\n{_escape(last[:120])}"


# ---------- Команды ----------


async def start_command(message: types.Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    await message.answer(
        "👋 Я мост MAX → Telegram (этап 2, на PyMax).\n"
        "Все новые сообщения MAX будут приходить сюда автоматически.\n"
        "Ответить — кнопка «💬 Ответить» под сообщением или /reply <chat_id>.\n"
        "Если MAX-сессия слетела — /reauth_sms: я попрошу у MAX новый код.\n"
        "Если нужно подключиться по сохранённой сессии — нажмите «📂 Загрузить сессию MAX».",
        reply_markup=main_reply_keyboard(),
    )


async def help_command(message: types.Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    await message.answer(
        "Команды:\n"
        "/start, /help — подсказка\n"
        "/status — состояние моста и MAX-сессии\n"
        "/chats — список MAX-чатов\n"
        "/reply <chat_id> — следующее сообщение уйдёт в этот чат\n"
        "/history <chat_id> [N=20] — последние N сообщений\n"
        "/reauth_sms — войти в MAX через SMS/2FA (отправит код в MAX)\n"
        "/upload_session — загрузить файл сессии MAX (bridge.db)\n"
        "/code <число> — ввести SMS-код или 2FA-пароль для текущего запроса\n"
        "/cancel — выйти из режима ответа / reauth / upload\n",
        reply_markup=main_reply_keyboard(),
    )


async def status_command(message: types.Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    try:
        s = await api.status()
    except Exception as exc:
        await message.answer(f"⚠️ Не удалось получить статус API: {exc}")
        return
    auth = s.get("auth", {})
    queue = s.get("queue", {})
    kind_label = {
        "sms": "SMS-код",
        "password": "2FA-пароль",
        None: "—",
    }.get(auth.get("pending_2fa_kind"), auth.get("pending_2fa_kind") or "—")
    pending_action = auth.get("pending_action") or "—"
    has_session = bool(auth.get("session_file_path"))
    text = (
        f"🔐 MAX auth: <b>{_escape(str(auth.get('status')))}</b>\n"
        f"   pending_2fa: {auth.get('pending_2fa_request_id') or '—'} "
        f"(тип: {kind_label})\n"
        f"   pending_action: <code>{_escape(str(pending_action))}</code>\n"
        f"   session_file: {'<code>' + _escape(str(auth.get('session_file_path'))) + '</code>' if has_session else '—'}\n"
        f"   last_2fa_at: {auth.get('last_2fa_request_at') or '—'}\n"
        f"   last_login: {auth.get('last_login_at') or '—'}\n"
        f"   error: {_escape(str(auth.get('last_error') or '—'))}\n"
        f"📬 Недоставлено: <b>{s.get('undelivered')}</b>\n"
        f"💬 Чатов в кэше: <b>{s.get('chats')}</b>\n"
        f"📤 Очередь отправки: pending={queue.get('pending')} "
        f"in_progress={queue.get('in_progress')} sent={queue.get('sent')} failed={queue.get('failed')}"
    )
    await message.answer(text, parse_mode="HTML")


async def chats_command(message: types.Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    try:
        chats = await api.list_chats()
    except Exception as exc:
        await message.answer(f"⚠️ Не удалось получить список чатов: {exc}")
        return
    if not chats:
        await message.answer("Пока нет ни одного чата. Откройте какой-нибудь чат в MAX.")
        return
    for c in chats[:30]:
        await message.answer(_format_chat(c), parse_mode="HTML")


async def history_command(message: types.Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Использование: /history <chat_id> [N=20]")
        return
    chat_id = args[1]
    limit = 20
    if len(args) >= 3:
        try:
            limit = max(1, min(int(args[2]), 100))
        except ValueError:
            limit = 20
    try:
        events = await api.list_events_for_chat(chat_id, limit=limit)
    except Exception as exc:
        await message.answer(f"⚠️ Ошибка: {exc}")
        return
    if not events:
        await message.answer("История пуста.")
        return
    for ev in events:
        try:
            await forward_event(message.bot, message.chat.id, ev)
            await message.answer(
                "—", reply_markup=event_inline_keyboard(ev.get("id", 0), ev.get("max_chat_id", ""))
            )
        except Exception as exc:
            await message.answer(f"⚠️ Не удалось переслать {ev.get('id')}: {exc}")


# ---------- /reauth_sms — войти в MAX через SMS/2FA ----------
#
# ВАЖНО: в новой модели этот хэндлер НЕ стирает сессию сам — он просто
# кладёт ``pending_action="sms"`` в БД. Supervisor при следующей итерации
# стирает cache, поднимает Client с SmsAuthFlow, тот запрашивает SMS-код
# через /auth/2fa/request → бот видит pending_2fa_request_id и просит
# владельца прислать /code <число>.


async def reauth_sms_command(message: types.Message, state: FSMContext) -> None:
    if not _is_allowed(message.from_user.id):
        return await _reject(message)

    logger.info("reauth_sms requested by uid=%s", message.from_user.id)
    await message.answer(
        "🔐 Запрашиваю у MAX SMS-авторизацию…\n"
        "• Если в кэше есть живая сессия — она будет стёрта.\n"
        "• Как только MAX пришлёт SMS или попросит пароль — пришлю уведомление.\n"
        "• Введите код или пароль командой /code <число>.\n"
        "• Отменить можно кнопкой «⛔ Отмена» в сообщении с меню."
    )

    try:
        await api.post_auth_action("sms")
    except Exception as exc:
        logger.warning("post_auth_action(sms) failed: %s", exc)
        await message.answer(f"⚠️ Не удалось передать команду API: {exc}")
        return

    await message.answer("📨 Команда отправлена в supervisor. Жду ответа MAX (обычно 5–30 секунд).")
    await state.set_state(ReauthSmsState.waiting_code)


async def code_command(message: types.Message, state: FSMContext) -> None:
    """Ввод кода/пароля для текущего pending 2FA-запроса.

    Логика совпадает с прежней версией, но обновлена под новый auth-флоу:
    если status=auth_required без pending rid — просим владельца сначала
    нажать /reauth_sms (а не /code сразу).
    """
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Использование: /code <число>")
        return
    code = args[1].strip()
    if not code or len(code) < 4:
        await message.answer("Код слишком короткий.")
        return

    logger.info(
        "/code received from uid=%s code_len=%d", message.from_user.id, len(code)
    )

    try:
        s = await api.status()
    except Exception as exc:
        logger.warning("code_command: api.status() failed: %s", exc)
        await message.answer(f"⚠️ API: {exc}")
        return
    auth = s.get("auth") or {}
    rid = auth.get("pending_2fa_request_id")
    pending_kind = (auth.get("pending_2fa_kind") or "").lower() or "?"
    last_2fa_at = auth.get("last_2fa_request_at")
    status = auth.get("status")

    if not rid:
        recent = False
        if last_2fa_at:
            try:
                from datetime import datetime, timezone, timedelta
                ts_raw = last_2fa_at
                if isinstance(ts_raw, str):
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                else:
                    ts = ts_raw
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                recent = (datetime.now(timezone.utc) - ts) < timedelta(minutes=10)
            except Exception:
                recent = False

        if status == "ok":
            await message.answer(
                "✅ MAX уже залогинен. Код не нужен. Если сообщения не приходят — /status."
            )
        elif status == "auth_required":
            await message.answer(
                "🔐 MAX сейчас ожидает вашу команду.\n"
                "Нажмите /reauth_sms, чтобы начать SMS-авторизацию,\n"
                "или «📂 Загрузить сессию MAX», чтобы загрузить файл."
            )
        elif recent:
            await message.answer(
                "⏳ MAX ещё не успел зарегистрировать новый запрос кода. "
                "Подождите ~30 секунд и пришлите /code ещё раз."
            )
        else:
            await message.answer(
                "Сейчас MAX не запрашивает код. Возможно, сессия ещё жива — попробуйте позже. "
                "Если это после /reauth_sms — подождите 30 секунд и пришлите /code ещё раз."
            )
        return

    try:
        await api.put_2fa(request_id=rid, code=code)
        logger.info(
            "/code forwarded to api: rid=%s uid=%s code_len=%d kind=%s",
            rid, message.from_user.id, len(code), pending_kind,
        )
        kind_label = {
            "sms": "SMS-код",
            "password": "2FA-пароль",
        }.get(pending_kind, "код")
        await message.answer(
            f"✅ {kind_label} отправлен (request_id={rid}, kind={pending_kind}). "
            "Дождитесь логина MAX."
        )
    except Exception as exc:
        logger.warning("code_command: put_2fa failed rid=%s: %s", rid, exc)
        await message.answer(f"⚠️ Не удалось передать код: {exc}")
        return
    await state.clear()


# ---------- /cancel — единый сброс FSM ----------


async def cancel_command(message: types.Message, state: FSMContext) -> None:
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    await state.clear()
    await message.answer("Ок, вышел из режима.")


# ---------- Reply keyboard buttons ----------


async def button_status(message: types.Message) -> None:
    await status_command(message)


async def button_chats(message: types.Message) -> None:
    await chats_command(message)


async def button_help(message: types.Message) -> None:
    await help_command(message)


async def button_listen(message: types.Message) -> None:
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    try:
        s = await api.status()
        auth = s.get("auth", {}).get("status", "unknown")
        undelivered = s.get("undelivered", 0)
    except Exception as exc:
        await message.answer(f"⚠️ API: {exc}")
        return
    await message.answer(
        f"🔄 MAX-сессия: <b>{_escape(str(auth))}</b>\n"
        f"📬 Недоставленных событий: <b>{undelivered}</b>\n"
        "Слушаю автоматически. Команда /chats — список диалогов.",
        parse_mode="HTML",
    )


async def button_upload_session(message: types.Message, state: FSMContext) -> None:
    """Обработчик reply-кнопки «📂 Загрузить сессию MAX»."""
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
    """Команда /upload_session — то же, что и reply-кнопка."""
    await button_upload_session(message, state)


# ---------- Reply FSM (отправка в MAX) ----------


async def reply_command(message: types.Message, state: FSMContext) -> None:
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Использование: /reply <chat_id>")
        return
    await state.set_state(ReplyState.waiting_text)
    await state.update_data(target_chat_id=args[1])
    await message.answer(
        f"✍️ Введите сообщение для чата <code>{_escape(args[1])}</code>.\n"
        "Можно отправить фото/видео/документ — всё уйдёт туда.\n"
        "/cancel — выйти.",
        parse_mode="HTML",
    )


async def reply_text(message: types.Message, state: FSMContext) -> None:
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    data = await state.get_data()
    target = data.get("target_chat_id")
    if not target:
        await state.clear()
        return
    text = message.text or ""
    try:
        res = await api.enqueue_send(
            target_chat_id=target, kind="text", text=text, created_by=message.from_user.id
        )
        await message.answer(
            f"✅ Отправлено в очередь (id={res.get('id')}). Дождитесь подтверждения от MAX."
        )
    except Exception as exc:
        await message.answer(f"⚠️ Ошибка постановки в очередь: {exc}")
    await state.clear()


async def reply_media(message: types.Message, state: FSMContext) -> None:
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
        )
        await message.answer(
            f"📨 Медиа поставлено в очередь (id={res.get('id')}, {kind}). "
            "Дождитесь подтверждения от MAX."
        )
    except Exception as exc:
        await message.answer(f"⚠️ Ошибка: {exc}")
    await state.clear()


# ---------- Приём файла сессии MAX (FSM UploadSessionState) ----------


async def upload_session_file_handler(message: types.Message, state: FSMContext) -> None:
    """Хэндлер на документ в состоянии ``UploadSessionState.waiting_file``.

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


# ---------- Inline callbacks: reply/showid/history + auth-action ----------


async def reply_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Обработчик inline-кнопки «💬 Ответить».

    В ``callback_data`` лежит только короткий ``event_id`` (из-за 64-байтного
    лимита Telegram). Реальный ``max_chat_id`` достаём из БД через
    ``api.get_event(event_id)``.
    """
    if not callback.from_user or not _is_allowed(callback.from_user.id):
        return await callback.answer("⛔", show_alert=True)
    if not callback.data or ":" not in callback.data:
        return await callback.answer("⚠️", show_alert=True)
    try:
        _, raw_id = callback.data.split(":", 1)
        event_id = int(raw_id)
    except (ValueError, IndexError):
        return await callback.answer("⚠️ битый callback", show_alert=True)
    try:
        ev = await api.get_event(event_id)
    except Exception as exc:
        logger.warning("reply_callback get_event(%s) failed: %s", event_id, exc)
        return await callback.answer("⚠️ API", show_alert=True)
    if not ev:
        return await callback.answer("⚠️ событие не найдено", show_alert=True)
    chat_id = ev.get("max_chat_id") or ""
    if not chat_id:
        return await callback.answer("⚠️ пустой chat_id", show_alert=True)
    await state.set_state(ReplyState.waiting_text)
    await state.update_data(target_chat_id=chat_id)
    await callback.answer()
    await callback.message.answer(
        f"✍️ Введите сообщение для чата <code>{_escape(chat_id)}</code> "
        "(или пришлите фото/видео/документ).\n/cancel — выйти.",
        parse_mode="HTML",
    )


async def showid_callback(callback: types.CallbackQuery) -> None:
    if not callback.data or ":" not in callback.data:
        return await callback.answer("⚠️", show_alert=True)
    try:
        _, raw_id = callback.data.split(":", 1)
        event_id = int(raw_id)
    except (ValueError, IndexError):
        return await callback.answer("⚠️ битый callback", show_alert=True)
    try:
        ev = await api.get_event(event_id)
    except Exception as exc:
        logger.warning("showid_callback get_event(%s) failed: %s", event_id, exc)
        return await callback.answer("⚠️ API", show_alert=True)
    chat_id = (ev or {}).get("max_chat_id") or "?"
    await callback.answer(f"ID: {chat_id}", show_alert=True)


async def history_callback(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not _is_allowed(callback.from_user.id):
        return await callback.answer("⛔", show_alert=True)
    if not callback.data or ":" not in callback.data:
        return await callback.answer("⚠️", show_alert=True)
    try:
        _, raw_id = callback.data.split(":", 1)
        event_id = int(raw_id)
    except (ValueError, IndexError):
        return await callback.answer("⚠️ битый callback", show_alert=True)
    try:
        ev = await api.get_event(event_id)
    except Exception as exc:
        logger.warning("history_callback get_event(%s) failed: %s", event_id, exc)
        return await callback.answer("⚠️ API", show_alert=True)
    if not ev:
        return await callback.answer("⚠️ событие не найдено", show_alert=True)
    chat_id = ev.get("max_chat_id") or ""
    if not chat_id:
        return await callback.answer("⚠️ пустой chat_id", show_alert=True)
    await callback.answer()
    try:
        events = await api.list_events_for_chat(chat_id, limit=20)
    except Exception as exc:
        await callback.message.answer(f"⚠️ Ошибка: {exc}")
        return
    if not events:
        await callback.message.answer("История пуста.")
        return
    for ev in events:
        try:
            await forward_event(callback.message.bot, callback.message.chat.id, ev)
        except Exception as exc:
            await callback.message.answer(f"⚠️ Не удалось переслать {ev.get('id')}: {exc}")


# ---------- /sessions — управление session-файлами ----------


async def sessions_command(message: types.Message) -> None:
    """Команда /sessions — показывает список доступных session-файлов"""
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
                from datetime import datetime
                dt = datetime.fromtimestamp(s["modified"])
                mod_time = f" • {dt.strftime('%d.%m %H:%M')}"

            current_mark = " ✅ **ТЕКУЩАЯ**" if current and s["path"] == current else ""
            text += f"• `{s['name']}` ({size_kb} КБ){mod_time}{current_mark}\n"

        text += f"\n**Всего:** {len(sessions)} файл(ов)"

        # Кнопки для быстрого выбора
        from app.keyboards import session_use_keyboard
        kb = session_use_keyboard([s["name"] for s in sessions[:8]])

        await message.answer(text, parse_mode="Markdown", reply_markup=kb)

    except Exception as exc:
        logger.error("sessions_command failed: %s", exc)
        await message.answer(f"❌ Не удалось получить список сессий: {exc}")


async def session_use_callback(callback: types.CallbackQuery) -> None:
    """Обработчик inline-кнопки «Использовать эту сессию»"""
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
    """Обработчик inline-кнопки «🔄 Обновить список» под /sessions.

    Перезапрашивает список session-файлов и редактирует текущее сообщение.
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
            from datetime import datetime
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
    """Обработчик reply-кнопки «📋 Сессии»."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    await sessions_command(message)


# ---------- Inline: auth:sms / auth:session / auth:upload / auth:cancel ----------


async def auth_action_callback(
        callback: types.CallbackQuery, state: FSMContext
) -> None:
    """Обработчик inline-кнопок выбора способа авторизации MAX.

    ``callback.data`` вида ``auth:<action>``, где ``action`` ∈
    {sms, session, cancel, upload}. Для «upload» дополнительно ставим
    FSM-состояние и просим прислать файл.
    """
    if not callback.from_user or not _is_allowed(callback.from_user.id):
        return await callback.answer("⛔", show_alert=True)
    if not callback.data:
        return await callback.answer()
    try:
        cb = AuthActionCallback.unpack(callback.data)
    except Exception:
        logger.warning("auth_action_callback: bad data %r", callback.data)
        return await callback.answer("⚠️", show_alert=True)

    action = (cb.action or "").lower()
    if action not in ("sms", "session", "cancel", "upload"):
        return await callback.answer(f"⚠️ неизвестное действие: {action}", show_alert=True)

    # Подтверждаем нажатие сразу, чтобы Telegram не показывал «часики».
    await callback.answer()

    if action == "upload":
        # Дополнительно ставим FSM и просим прислать файл.
        await state.set_state(UploadSessionState.waiting_file)
        await callback.message.answer(
            "📂 Пришлите <b>документом</b> файл сессии MAX "
            "(обычно <code>bridge.db</code>, до 50 МБ).\n"
            "/cancel — отмена.",
            parse_mode="HTML",
        )
        return

    # Для sms / session / cancel — шлём pending_action в api.
    pretty = {
        "sms": "🔐 SMS-авторизация",
        "session": "📂 Подключиться по сессии",
        "cancel": "⛔ Отмена",
    }.get(action, action)
    await callback.message.answer(f"{pretty}: отправляю команду supervisor'у…")

    try:
        await api.post_auth_action(action)
    except Exception as exc:
        logger.warning("post_auth_action(%s) failed: %s", action, exc)
        await callback.message.answer(f"⚠️ API: {exc}")
        return

    if action == "sms":
        await callback.message.answer(
            "📨 Запросил SMS у MAX. Жду ответа (5–30 секунд). "
            "Как только придёт код — пришлю /code."
        )
        await state.set_state(ReauthSmsState.waiting_code)
    elif action == "session":
        await callback.message.answer(
            "🔌 Поднимаю MAX Client по сохранённой сессии… "
            "Если сессия валидна — вход пройдёт без SMS."
        )
    elif action == "cancel":
        await callback.message.answer(
            "🛑 Отменил текущее действие. MAX вернётся в режим "
            "ожидания команды."
        )
        await state.clear()


# ---------- AuthWatcher: поллер auth_state ----------


class AuthWatcher:
    """Каждые N секунд проверяет auth_state и:

    * при status=ok — пуш «✅ MAX: вход выполнен успешно»;
    * при status=need_2fa — пуш с просьбой прислать /code;
    * при status=rate_limited — пуш с подсказкой про cooldown;
    * при status=auth_required — пуш с inline-меню «Что делать?»;
    * при status=session_attached — пуш с inline-меню
      «📂 Подключиться по сессии»;
    * потребляет ``notify_message`` (если max-процесс положил одноразовое
      сообщение, например «session uploaded, size=…») и пересылает его
      владельцу.
    """

    POLL_INTERVAL = 3.0

    API_ERROR_WARN_BUDGET = 3

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._notified_request_id: Optional[int] = None
        self._last_known_status: Optional[str] = None
        self._last_known_pending_action: Optional[str] = None
        self._last_known_session_path: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._api_error_count: int = 0

    async def _notify_owner(self, text: str, reply_markup=None) -> None:
        for uid in settings.allowed_tg_user_ids:
            try:
                await self.bot.send_message(
                    uid, text, reply_markup=reply_markup
                )
            except Exception as exc:
                logger.warning("notify uid=%s failed: %s", uid, exc)

    @staticmethod
    def _prompt_text(kind: Optional[str]) -> str:
        if kind == "password":
            return (
                "🔐 MAX запросил <b>2FA-пароль</b>.\n"
                "Пришлите: <code>/code <ваш_пароль></code>"
            )
        return (
            "🔐 MAX прислал <b>SMS-код</b>.\n"
            "Посмотрите SMS на номер MAX и пришлите: <code>/code <число></code>"
        )

    @staticmethod
    def _has_session_on_disk(cache_dir: str) -> bool:
        """Локальная проверка на случай, если в auth_state.status ещё не
        успел обновиться, а session-файл уже залит напрямую на сервер.

        Проверяем не только ``bridge.db``, но и любой ``*.db`` в кэше
        (владелец мог положить файл с произвольным именем).
        """
        try:
            p = Path(cache_dir) / "bridge.db"
            if p.is_file():
                return True
            cache = Path(cache_dir)
            if not cache.is_dir():
                return False
            for cand in cache.glob("*.db"):
                if cand.is_file() and not cand.name.endswith(("-shm", "-wal")):
                    return True
            return False
        except Exception:
            return False

    def _session_present(self, auth: dict) -> bool:
        if auth.get("session_file_path"):
            return True
        # Фолбэк на cache_dir (владелец мог положить файл руками).
        return self._has_session_on_disk(settings.cache_dir)

    async def _tick(self) -> None:
        try:
            s = await api.status()
            self._api_error_count = 0
        except Exception as exc:
            self._api_error_count += 1
            if self._api_error_count <= self.API_ERROR_WARN_BUDGET:
                logger.warning(
                    "auth_watcher api error (%d/%d): %s",
                    self._api_error_count, self.API_ERROR_WARN_BUDGET, exc,
                )
            else:
                logger.debug("auth_watcher api error: %s", exc)
            return

        auth = s.get("auth") or {}
        status = auth.get("status") or "unknown"
        rid = auth.get("pending_2fa_request_id")
        kind = auth.get("pending_2fa_kind") or "sms"
        last_err = (auth.get("last_error") or "").lower()
        pending_action = auth.get("pending_action")
        session_path = auth.get("session_file_path")
        notify_message = auth.get("notify_message")

        # 1) Сначала потребляем одноразовое уведомление от supervisor'а —
        #    оно приоритетнее статусных сообщений, т.к. часто объясняет,
        #    что только что произошло (session uploaded, wipe и т.п.).
        if notify_message:
            self._last_consumed_notify = notify_message
            await self._notify_owner(notify_message)
            try:
                await api.consume_notify()
            except Exception as exc:
                logger.debug("consume_notify failed: %s", exc)
            return

        # 2) Реакция на смену основного статуса.
        if status != self._last_known_status:
            prev = self._last_known_status
            self._last_known_status = status

            if status == "ok":
                self._notified_request_id = None
                await self._notify_owner("✅ MAX: вход выполнен успешно.")
            elif status == "need_2fa":
                if rid and rid != self._notified_request_id:
                    self._notified_request_id = rid
                    await self._notify_owner(self._prompt_text(kind))
            elif status == "rate_limited":
                hint = ""
                if "limit.violate" in last_err or "rate" in last_err:
                    hint = " MAX ограничил частоту запросов — попробуем снова через ~10 мин."
                await self._notify_owner(
                    "⚠️ MAX временно ограничил авторизацию." + hint +
                    "\nКак только cooldown пройдёт, я пришлю уведомление."
                )
            elif status == "auth_required":
                # Главное меню — выбор способа авторизации.
                has_session = self._session_present(auth)
                kb = auth_choice_keyboard(
                    show_upload=not has_session,
                    show_session_connect=has_session,
                )
                await self._notify_owner(
                    "🔐 MAX не подключён.\n"
                    "• «🔐 SMS-авторизация» — стартовать новый Client и получить SMS.\n"
                    + (
                        "• «📂 Подключиться по сессии» — в кэше уже есть файл, попробуем его.\n"
                        if has_session else
                        "• «📎 Загрузить файл сессии» — сначала пришлите bridge.db.\n"
                    )
                    + "Выберите действие:",
                    reply_markup=kb,
                )
            elif status == "session_attached":
                # Владелец (или supervisor) обнаружил session-файл — ждём подтверждения.
                kb = auth_choice_keyboard(
                    show_upload=False,
                    show_session_connect=True,
                )
                path_disp = session_path or str(
                    Path(settings.cache_dir) / "bridge.db"
                )
                await self._notify_owner(
                    f"📥 В кэше MAX обнаружен session-файл:\n<code>{_escape(path_disp)}</code>\n"
                    "Нажмите «📂 Подключиться по сессии», чтобы войти.\n"
                    "Или «🔐 SMS-авторизация», чтобы войти по SMS (старая сессия будет стёрта).",
                    reply_markup=kb,
                )
            elif status == "unknown":
                if not rid:
                    self._notified_request_id = None
            # prev — только для возможного расширения логирования.
            _ = prev

        # 3) На случай, если status не менялся, но rid обновился.
        elif status == "need_2fa" and rid and rid != self._notified_request_id:
            self._notified_request_id = rid
            await self._notify_owner(self._prompt_text(kind))
        elif status == "unknown" and rid and rid != self._notified_request_id:
            self._notified_request_id = rid
            await self._notify_owner(self._prompt_text(kind))

        # 4) Если status=auth_required, а pending_action уже выставлен —
        #    дадим знать, что команда принята supervisor'ом (один раз на
        #    смену).
        if status == "auth_required" and pending_action and pending_action != self._last_known_pending_action:
            self._last_known_pending_action = pending_action
            label = {
                "sms": "📨 SMS-авторизация",
                "session": "🔌 Подключение по сессии",
                "cancel": "🛑 Отмена",
            }.get(pending_action, pending_action)
            await self._notify_owner(
                f"⏳ Команда «{label}» принята в очередь. Жду реакции supervisor'а."
            )
        elif not pending_action:
            self._last_known_pending_action = None

        # 5) Следим за появлением session-файла: если путь изменился —
        #    упомянем владельцу. Полноценное уведомление уйдёт через
        #    status=session_attached (выставляется /admin/session/upload).
        if session_path and session_path != self._last_known_session_path:
            self._last_known_session_path = session_path
            if status not in ("session_attached",):
                await self._notify_owner(
                    f"📂 Путь к session-файлу обновился: <code>{_escape(session_path)}</code>"
                )
        elif not session_path:
            self._last_known_session_path = None

    async def _run(self) -> None:
        logger.info("AuthWatcher started (poll=%.1fs)", self.POLL_INTERVAL)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("auth_watcher tick error: %s", exc)
            await asyncio.sleep(self.POLL_INTERVAL)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="auth-watcher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


# ---------- Регистрация ----------


def register_handlers(dp: Dispatcher) -> None:
    dp.message.register(start_command, Command("start"))
    dp.message.register(help_command, Command("help"))
    dp.message.register(status_command, Command("status"))
    dp.message.register(chats_command, Command("chats"))
    dp.message.register(history_command, Command("history"))
    dp.message.register(reauth_sms_command, Command("reauth_sms"))
    dp.message.register(code_command, Command("code"))
    dp.message.register(reply_command, Command("reply"))
    dp.message.register(cancel_command, Command("cancel"))
    dp.message.register(upload_session_command, Command("upload_session"))
    dp.message.register(sessions_command, Command("sessions"))

    dp.message.register(button_status, F.text == "ℹ️ Статус")
    dp.message.register(button_chats, F.text == "📚 Чаты")
    dp.message.register(button_help, F.text == "🆘 Помощь")
    dp.message.register(button_listen, F.text == "📥 Слушать MAX")
    dp.message.register(
        button_upload_session, F.text == "📂 Загрузить сессию MAX"
    )
    dp.message.register(button_sessions, F.text == "📋 Сессии")

    # FSM: загрузка session-файла
    dp.message.register(
        upload_session_file_handler,
        UploadSessionState.waiting_file,
        F.content_type == "document",
    )

    dp.message.register(
        reply_text, ReplyState.waiting_text, F.content_type == "text"
    )
    dp.message.register(
        reply_media,
        ReplyState.waiting_text,
        F.content_type.in_({"photo", "video", "document"}),
    )

    dp.callback_query.register(reply_callback, F.callback_data.startswith("reply:"))
    dp.callback_query.register(showid_callback, F.callback_data.startswith("showid:"))
    dp.callback_query.register(history_callback, F.callback_data.startswith("history:"))
    dp.callback_query.register(
        session_use_callback, F.callback_data.startswith("session_use:")
    )
    dp.callback_query.register(
        sessions_refresh_callback, F.callback_data == "sessions_refresh"
    )
    dp.callback_query.register(
        auth_action_callback, AuthActionCallback.filter()
    )
