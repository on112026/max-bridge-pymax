"""Базовые команды Telegram-бота: ``/start``, ``/help``, ``/status``, ``/chats``, ``/history``.

Эти команды не меняют состояние моста — только показывают информацию
владельцу. Поэтому все они собраны здесь, в ``basic.py``.

* ``/start`` / ``/help`` — onboarding-сообщение с подсказкой по командам.
* ``/status`` — текущее состояние моста (auth, queue, undelivered,
  stale-топики, активные джобы синка).
* ``/chats`` — список MAX-чатов (до 30 штук за один ответ).
* ``/history`` — последние N сообщений из конкретного MAX-чата.

Также здесь живут «reply keyboard»-кнопки, которые по сути дублируют
команды (``ℹ️ Статус`` → ``/status``, ``📚 Чаты`` → ``/chats``,
``🆘 Помощь`` → ``/help``, ``📥 Слушать MAX`` → краткая сводка).
"""

from __future__ import annotations

import logging

from aiogram import types

from app.api_client import api
from app.handlers._common import _escape, _format_chat, _is_allowed, _reject
from app.keyboards import event_inline_keyboard, main_reply_keyboard
from app.sender import forward_event
from shared import db as shared_db

logger = logging.getLogger(__name__)


# ---------- Команды ----------


async def start_command(message: types.Message) -> None:
    """``/start`` — onboarding-сообщение с подсказкой по командам."""
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
    """``/help`` — список всех доступных команд."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    await message.answer(
        "Команды:\n"
        "/start, /help — подсказка\n"
        "/status — состояние моста и MAX-сессии\n"
        "/chats — список MAX-чатов\n"
        "/reply <chat_id> — следующее сообщение уйдёт в этот чат\n"
        "/history <chat_id> [N=20] — последние N сообщений\n"
        "/setup — инструкция по подключению supergroup\n"
        "/autosetup — вызвать в группе: бот сам определит chat_id и привяжет её\n"
        "/setgroup <chat_id> — ручное подключение supergroup по её id\n"
        "/getlink — получить invite-ссылку на привязанную группу\n"
        "/reauth_sms — войти в MAX через SMS/2FA (отправит код в MAX)\n"
        "/upload_session — загрузить файл сессии MAX (bridge.db)\n"
        "/code <число> — ввести SMS-код или 2FA-пароль для текущего запроса\n"
        "/cancel — выйти из режима ответа / reauth / upload\n"
        "/chatops — управление чатами MAX: /join, /resolve, /invite, /search_user, /pending, /approve, /decline\n",
        reply_markup=main_reply_keyboard(),
    )


async def status_command(message: types.Message) -> None:
    """``/status`` — состояние моста и MAX-сессии."""
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
    # Доп. секция: топики без MAX-чата (stale) и активные джобы синка.
    try:
        owner_uid = int(message.from_user.id)
        stale = shared_db.count_topics_for_owner(owner_uid)
        if stale > 0:
            text += (
                f"\n⚠️ Топиков без MAX-чата: <b>{stale}</b>. "
                f"Используйте /prune_topics."
            )
    except Exception as exc:
        logger.debug("status_command: stale count failed: %s", exc)
    try:
        job_stats = await api.topic_jobs_stats()
        pending_create = int(job_stats.get("pending_create") or 0)
        pending_rename = int(job_stats.get("pending_rename") or 0)
        in_progress_create = int(job_stats.get("in_progress_create") or 0)
        in_progress_rename = int(job_stats.get("in_progress_rename") or 0)
        total_active = (
            pending_create + pending_rename
            + in_progress_create + in_progress_rename
        )
        if total_active > 0:
            text += (
                f"\n🛠 Sync топиков: pending(create={pending_create}, rename={pending_rename}), "
                f"in_progress(create={in_progress_create}, rename={in_progress_rename})"
            )
    except Exception as exc:
        logger.debug("status_command: topic_jobs_stats failed: %s", exc)
    await message.answer(text, parse_mode="HTML")


async def chats_command(message: types.Message) -> None:
    """``/chats`` — список MAX-чатов (до 30 за один ответ)."""
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
    """``/history <chat_id> [N=20]`` — последние N сообщений из MAX-чата.

    Дополнительно помечает чат как прочитанный (``mark_chat_read_up_to``),
    чтобы MAX-процесс синхронизировал ``client.read_message``.
    """
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
    # Помечаем чат как прочитанный (пользователь явно запросил историю).
    try:
        await api.mark_chat_read_up_to(chat_id=chat_id)
    except Exception as exc:
        logger.warning("mark_chat_read_up_to failed: %s", exc)
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


# ---------- Reply keyboard buttons (делегаты к командам) ----------


async def button_status(message: types.Message) -> None:
    """Reply-кнопка «ℹ️ Статус» → ``status_command``."""
    await status_command(message)


async def button_chats(message: types.Message) -> None:
    """Reply-кнопка «📚 Чаты» → ``chats_command``."""
    await chats_command(message)


async def button_help(message: types.Message) -> None:
    """Reply-кнопка «🆘 Помощь» → ``help_command``."""
    await help_command(message)


async def button_listen(message: types.Message) -> None:
    """Reply-кнопка «📥 Слушать MAX» — короткая сводка, что бот слушает MAX.

    Не показывает полный статус (для этого есть ``/status``), а только
    быстрый «всё ок, я работаю» — чтобы владелец не дёргал ``/status``
    каждые 30 секунд.
    """
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