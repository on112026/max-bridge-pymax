"""Хэндлеры команд и callback'ов Telegram-бота (этап 2, PyMax).

Удалены:
- /reauth (старый headful-режим с управлением браузером)
- HeadfulState, hf_* callbacks, headful_main_keyboard
- /vnc, noVNC, скриншоты — больше не нужны, PyMax авторизуется через SMS/2FA

Добавлены:
- /reauth_sms — перевод MAX в reauth (стирает сессию), затем max-процесс
  начинает заново логин по номеру и запрашивает SMS/2FA через бота
- /code <N> — ввод SMS-кода/пароля для текущего pending-запроса
- AuthWatcher — фоновый поллер auth_state, шлёт владельцу push о запросе кода
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from typing import Any, Dict

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from app.api_client import api
from app.config import settings
from app.keyboards import event_inline_keyboard, main_reply_keyboard
from app.sender import forward_event
from app.states import ReplyState, ReauthSmsState

logger = logging.getLogger(__name__)

MAX_TG_DOWNLOAD = 49 * 1024 * 1024


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
        "Если MAX-сессия слетела — /reauth_sms: я попрошу у MAX новый код.",
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
        "/reauth_sms — сбросить MAX-сессию и войти заново (придёт SMS или 2FA)\n"
        "/code <число> — ввести SMS-код или 2FA-пароль для текущего запроса\n"
        "/cancel — выйти из режима ответа / reauth\n",
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
    text = (
        f"🔐 MAX auth: <b>{_escape(str(auth.get('status')))}</b>\n"
        f"   pending_2fa: {auth.get('pending_2fa_request_id') or '—'} "
        f"(тип: {kind_label})\n"
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


# ---------- /reauth_sms — вход в MAX заново через SMS/2FA ----------


async def reauth_sms_command(message: types.Message, state: FSMContext) -> None:
    """Стереть локальную сессию MAX и дать сигнал max-процессу начать новый логин.

    max-процесс перезапустит PyMax Client с нуля, вызовет SmsAuthFlow,
    тот попросит SMS-код → max-процесс положит pending-запрос в БД →
    бот увидит это в AuthWatcher и пришлёт владельцу уведомление
    «MAX ждёт SMS-код, пришлите /code <число>».
    """
    if not _is_allowed(message.from_user.id):
        return await _reject(message)

    await message.answer(
        "🔐 Запускаю reauth MAX…\n"
        "Сессия MAX на сервере будет сброшена. Как только MAX пришлёт SMS или попросит пароль — "
        "я пришлю уведомление сюда.\n"
        "Введите код или пароль командой /code <число>."
    )

    # 1) Сбросить auth_state на unknown + записать запрос «need_sms» (используем существующий need_2fa)
    try:
        await api.post_auth_state(status="need_2fa", error="reauth requested by owner")
    except Exception as exc:
        await message.answer(f"⚠️ Не удалось обновить auth_state: {exc}")
        return

    # 2) Попытаемся «разбудить» max-процесс: для этого достаточно перевести
    #    auth_state в «need_2fa» — supervisor в max-процессе при следующем цикле
    #    поймёт, что нужно пересоздать Client.
    #    ВАЖНО: фактическое стирание сессии делает max/app/supervisor.py —
    #    он следит за auth_state и при need_2fa+ошибке стирает cache и
    #    перезапускает Client.
    await state.set_state(ReauthSmsState.waiting_code)


async def code_command(message: types.Message, state: FSMContext) -> None:
    """Ввод кода/пароля для текущего pending 2FA-запроса."""
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

    try:
        s = await api.status()
    except Exception as exc:
        await message.answer(f"⚠️ API: {exc}")
        return
    rid = (s.get("auth") or {}).get("pending_2fa_request_id")
    if not rid:
        await message.answer(
            "Сейчас MAX не запрашивает код. Возможно, сессия ещё жива — попробуйте позже."
        )
        return
    try:
        await api.put_2fa(request_id=rid, code=code)
        await message.answer(
            f"✅ Код отправлен (request_id={rid}). Дождитесь логина MAX."
        )
    except Exception as exc:
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


# ---------- Inline callbacks ----------


async def reply_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not _is_allowed(callback.from_user.id):
        return await callback.answer("⛔", show_alert=True)
    if not callback.data or ":" not in callback.data:
        return await callback.answer()
    _, chat_id = callback.data.split(":", 1)
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
        return await callback.answer()
    _, chat_id = callback.data.split(":", 1)
    await callback.answer(f"ID: {chat_id}", show_alert=True)


async def history_callback(callback: types.CallbackQuery) -> None:
    if not callback.from_user or not _is_allowed(callback.from_user.id):
        return await callback.answer("⛔", show_alert=True)
    if not callback.data or ":" not in callback.data:
        return await callback.answer()
    _, chat_id = callback.data.split(":", 1)
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


# ---------- AuthWatcher: поллер auth_state ----------


class AuthWatcher:
    """Каждые N секунд проверяет auth_state. Если появился pending_2fa_request_id
    и владельцу ещё не ушло уведомление — шлёт push в Telegram.
    """

    POLL_INTERVAL = 3.0

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._notified_request_id: int | None = None
        self._last_known_status: str | None = None
        self._task: asyncio.Task | None = None

    async def _notify_owner(self, text: str) -> None:
        for uid in settings.allowed_tg_user_ids:
            try:
                await self.bot.send_message(uid, text)
            except Exception as exc:
                logger.warning("notify uid=%s failed: %s", uid, exc)

    @staticmethod
    def _prompt_text(kind: str | None) -> str:
        """Разные подсказки в зависимости от типа запрошенного кода."""
        if kind == "password":
            return (
                "🔐 MAX запросил <b>2FA-пароль</b>.\n"
                "Пришлите: <code>/code <ваш_пароль></code>"
            )
        # по умолчанию — SMS
        return (
            "🔐 MAX прислал <b>SMS-код</b>.\n"
            "Посмотрите SMS на номер MAX и пришлите: <code>/code <число></code>"
        )

    async def _tick(self) -> None:
        try:
            s = await api.status()
        except Exception as exc:
            # api может быть не готов — не спамим
            if not str(exc).startswith("ConnectError"):
                logger.debug("auth_watcher api error: %s", exc)
            return
        auth = s.get("auth") or {}
        status = auth.get("status") or "unknown"
        rid = auth.get("pending_2fa_request_id")
        kind = auth.get("pending_2fa_kind") or "sms"
        last_err = (auth.get("last_error") or "").lower()

        # Уведомляем о смене статуса (ok, need_2fa, rate_limited, unknown)
        if status != self._last_known_status:
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
            elif status == "unknown":
                self._notified_request_id = None
        elif status == "need_2fa" and rid and rid != self._notified_request_id:
            # на случай, если status не менялся, но rid новый
            self._notified_request_id = rid
            await self._notify_owner(self._prompt_text(kind))

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

    dp.message.register(button_status, F.text == "ℹ️ Статус")
    dp.message.register(button_chats, F.text == "📚 Чаты")
    dp.message.register(button_help, F.text == "🆘 Помощь")
    dp.message.register(button_listen, F.text == "📥 Слушать MAX")

    dp.message.register(
        reply_text, ReplyState.waiting_text, F.content_type == "text"
    )
    dp.message.register(
        reply_media,
        ReplyState.waiting_text,
        F.content_type.in_({"photo", "video", "document"}),
    )

    # reauth_sms: игнорируем текст, пока FSM активна — настоящий код вводится через /code
    dp.message.register(
        lambda m: m.answer("Введите код командой /code <число>, либо /cancel."),
        ReauthSmsState.waiting_code,
        F.content_type == "text",
    )

    dp.callback_query.register(reply_callback, F.callback_data.startswith("reply:"))
    dp.callback_query.register(showid_callback, F.callback_data.startswith("showid:"))
    dp.callback_query.register(history_callback, F.callback_data.startswith("history:"))