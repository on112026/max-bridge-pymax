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
import traceback
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from app.api_client import api
from app.config import settings
from app.keyboards import (
    AuthActionCallback,
    EventActionCallback,
    SessionUseCallback,
    auth_choice_keyboard,
    event_inline_keyboard,
    main_reply_keyboard,
)
from app.sender import forward_event
from app.states import ReplyState, ReauthSmsState, UploadSessionState
from app.keyboards import session_use_keyboard
from app.topics import ensure_forum_enabled, export_invite_link
from shared import db as shared_db

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


# ---------- /setup — создание приватной Telegram-группы для моста ----------


async def setup_command(message: types.Message) -> None:
    """Инструкция по подключению supergroup (forum) для пересылки событий MAX.

    ВНИМАНИЕ: Telegram Bot API **не позволяет ботам создавать supergroups**
    напрямую. Это всегда делается либо через клиентский MTProto (TDesktop,
    Telegram для Android/iOS), либо вручную в любом клиенте Telegram. После
    того как supergroup создана и бот добавлен в неё как админ, владелец
    присылает сюда ``chat_id`` через ``/setgroup <chat_id>``.

    Поведение:
    * Если supergroup уже привязана — присылает её текущий ``invite_link``
      и ``chat_id`` (для справки).
    * Если нет — пошаговая инструкция по созданию вручную.
    """
    if not _is_allowed(message.from_user.id):
        return await _reject(message)

    owner_uid = message.from_user.id
    existing = shared_db.get_supergroup_for_owner(owner_uid)
    if existing:
        link = existing.invite_link or "(см. /getlink)"
        await message.answer(
            "ℹ️ Supergroup уже подключена.\n\n"
            f"Название: <b>{_escape(existing.title or '—')}</b>\n"
            f"chat_id: <code>{existing.supergroup_chat_id}</code>\n"
            f"Топики (forum): {'включены ✅' if existing.is_forum_enabled else 'включите вручную ⚠️'}\n"
            f"🔗 Ссылка: {link}\n\n"
            "Если хотите сменить группу — выполните /setgroup <chat_id> ещё раз."
        )
        return

    await message.answer(
        "🛠 <b>Настройка supergroup для моста MAX ↔ Telegram</b>\n\n"
        "Telegram Bot API не умеет создавать supergroups напрямую, "
        "поэтому нужно создать группу вручную (в любом клиенте Telegram).\n\n"
        "<b>Шаги:</b>\n"
        "1. В Telegram создайте новую группу (меню «Создать группу»).\n"
        "2. <b>Включите топики</b>: Настройки группы → «Топики» (Topics).\n"
        "3. Добавьте этого бота в группу и сделайте <b>админом</b> "
        "(с правом «Управление топиками»).\n"
        "4. Узнайте <code>chat_id</code> группы. Проще всего: "
        "перешлите любое сообщение из группы боту @RawDataBot — "
        "в ответе будет поле <code>forward_from_chat.id</code> "
        "(формат <code>-100xxxxxxxxxx</code>).\n"
        "5. Вернитесь сюда и выполните:\n"
        "   <code>/setgroup <chat_id></code>\n\n"
        "После этого бот начнёт пересылать события из MAX в эту группу "
        "(каждый MAX-чат получит свой топик)."
    )


async def setgroup_command(message: types.Message) -> None:
    """Привязать существующую supergroup (forum) к владельцу.

    Использование: <code>/setgroup <chat_id></code>, где ``chat_id``
    имеет формат <code>-100xxxxxxxxxx</code>. Бот проверит, что он
    находится в группе и может создавать топики, после чего сохранит
    привязку в БД. EventPoller сразу подхватит накопившиеся события.
    """
    if not _is_allowed(message.from_user.id):
        return await _reject(message)

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer(
            "Использование: <code>/setgroup <chat_id></code>\n"
            "Формат <code>chat_id</code>: <code>-100xxxxxxxxxx</code> "
            "(можно узнать через @RawDataBot)."
        )
        return

    try:
        chat_id = int(args[1].strip())
    except ValueError:
        await message.answer("⚠️ chat_id должен быть числом (формат -100xxxxxxxxxx).")
        return

    # Проверим, что бот действительно состоит в этой группе и видит её.
    try:
        chat = await message.bot.get_chat(chat_id)
    except Exception as exc:
        await message.answer(
            f"⚠️ Не удалось получить чат <code>{chat_id}</code>: {_escape(str(exc))}\n"
            "Убедитесь, что бот добавлен в группу и имеет права админа."
        )
        return

    chat_type = getattr(chat, "type", None)
    if chat_type and "group" not in str(chat_type).lower():
        await message.answer(
            f"⚠️ Чат <code>{chat_id}</code> имеет тип <b>{_escape(str(chat_type))}</b>, "
            "а нужен supergroup с включёнными топиками."
        )
        return

    # Проверим, что бот — админ (нужны права на создание топиков).
    try:
        member = await message.bot.get_chat_member(chat_id, (await message.bot.get_me()).id)
        status = getattr(member, "status", None)
        if str(status) not in ("ChatMemberStatus.ADMINISTRATOR", "administrator", "creator"):
            await message.answer(
                "⚠️ Бот должен быть <b>админом</b> группы (с правом «Manage Topics»). "
                "Сделайте бота админом и попробуйте /setgroup ещё раз."
            )
            return
    except Exception as exc:
        logger.warning("setgroup: get_chat_member failed for %s: %s", chat_id, exc)
        await message.answer(
            f"⚠️ Не удалось проверить права бота в группе: {_escape(str(exc))}"
        )
        return

    # Включим forum mode (если ещё не включён).
    forum_ok = await ensure_forum_enabled(message.bot, chat_id)
    if not forum_ok:
        await message.answer(
            "⚠️ Не удалось включить топики автоматически. Включите их вручную:\n"
            "Настройки группы → Топики → Включить. Затем повторите /setgroup."
        )
        # Сохраняем всё равно, чтобы владелец мог доделать настройки и вернуться.
        # Если топики не включены — EventPoller будет падать, но данные сохранятся.

    # Получаем/создаём invite link.
    invite_link = await export_invite_link(message.bot, chat_id)

    title = getattr(chat, "title", None) or "MAX Bridge 🔒"

    shared_db.create_supergroup(
        owner_user_id=message.from_user.id,
        supergroup_chat_id=chat_id,
        title=title,
        invite_link=invite_link,
        is_forum_enabled=forum_ok,
    )

    # Сбрасываем флаг «уже подсказали про /setup» в AuthWatcher (если он
    # запущен в этом процессе). Чтобы при следующем всплеске undelivered
    # снова получить подсказку, если группу позже удалят.
    w = AuthWatcher.get_active()
    if w is not None:
        try:
            w._supergroup_prompted_for.discard(message.from_user.id)
        except Exception as exc:
            logger.debug("setgroup: reset prompt flag failed: %s", exc)

    await message.answer(
        f"✅ Supergroup подключена!\n\n"
        f"Название: <b>{_escape(title)}</b>\n"
        f"chat_id: <code>{chat_id}</code>\n"
        f"Топики (forum): {'включены ✅' if forum_ok else '⚠️ включите вручную'}\n"
        f"🔗 Invite: {invite_link or '(сгенерируйте вручную через /getlink)'}\n\n"
        "Бот начнёт пересылать события из MAX в эту группу (каждый MAX-чат "
        "получит свой топик). Уже накопленные события будут доставлены "
        "автоматически в течение пары секунд."
    )

    # Если были события «undelivered» — форсим один tick EventPoller'а
    # нельзя (поллер сам крутится), но владельцу полезно увидеть мгновенный
    # фидбек. Поэтому после /setgroup сразу дёрнем api.status и сообщим.
    try:
        s = await api.status()
        undelivered = s.get("undelivered", 0)
        if undelivered:
            await message.answer(
                f"📬 Сейчас в очереди <b>{undelivered}</b> непрочитанных событий — "
                "через пару секунд они появятся в группе в своих топиках."
            )
    except Exception as exc:
        logger.debug("setgroup: status check failed: %s", exc)


async def getlink_command(message: types.Message) -> None:
    """Получить/пересоздать invite-ссылку на приватную группу."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    sg = shared_db.get_supergroup_for_owner(message.from_user.id)
    if not sg:
        await message.answer(
            "ℹ️ Группа ещё не создана. Используйте /setup."
        )
        return
    new_link = await export_invite_link(message.bot, sg.supergroup_chat_id)
    if new_link:
        shared_db.update_supergroup_invite_link(message.from_user.id, new_link)
    await message.answer(
        f"🔗 Ссылка на группу «{_escape(sg.title or '—')}»:\n"
        f"{new_link or sg.invite_link or '(не удалось получить)'}"
    )


# ---------- Reply FSM (отправка в MAX) ----------


async def reply_command(message: types.Message, state: FSMContext) -> None:
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


async def reply_text(message: types.Message, state: FSMContext) -> None:
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


# ---------- Inline callbacks: event-action (reply/showid/history) + auth-action ----------


async def _resolve_event_chat_id(event_id: int) -> tuple[Optional[str], Optional[Message]]:
    """Достаёт ``max_chat_id`` события из БД через ``api.get_event``.

    Возвращает ``(chat_id, alert_message)``. Если что-то пошло не так,
    ``alert_message`` — готовое сообщение с эмодзи, которое хэндлер
    должен отправить пользователю.
    """
    try:
        ev = await api.get_event(event_id)
    except AttributeError:
        logger.error(
            "api.get_event MISSING (api_client.py без этого метода)",
            exc_info=True,
        )
        return None, "⚠️ api_client.py без метода get_event — перезапустите контейнер"
    except Exception as exc:
        logger.error(
            "api.get_event(%s) FAILED: %s", event_id, exc, exc_info=True,
        )
        return None, f"⚠️ API: {exc}"
    if not ev:
        return None, "⚠️ событие не найдено"
    chat_id = ev.get("max_chat_id") or ""
    if not chat_id:
        return None, "⚠️ пустой chat_id"
    return chat_id, None


async def event_action_callback(
        callback: types.CallbackQuery, state: FSMContext
) -> None:
    """Обработчик inline-кнопок под сообщением из MAX.

    Использует единый ``EventActionCallback.filter()`` (фабричный фильтр
    CallbackData-класса) — это критично в aiogram 3.15, где смешивание
    ``F.callback_data.startswith(...)`` с ``CallbackData.filter()``
    ломает фильтрацию callback_query.

    Действие (``reply`` / ``showid`` / ``history``) берётся из
    распакованного callback_data.
    """
    logger.info(
        "event_action_callback ENTERED: data=%r from uid=%s chat=%s msg_id=%s",
        callback.data,
        callback.from_user.id if callback.from_user else None,
        callback.message.chat.id if callback.message else None,
        callback.message.message_id if callback.message else None,
    )
    if not callback.from_user or not _is_allowed(callback.from_user.id):
        logger.warning(
            "event_action_callback: user %s not allowed",
            callback.from_user.id if callback.from_user else None,
        )
        return await callback.answer("⛔", show_alert=True)

    # Распаковываем callback_data через EventActionCallback.
    try:
        cb = EventActionCallback.unpack(callback.data)
    except Exception as exc:
        logger.error(
            "event_action_callback: failed to unpack callback.data=%r: %s",
            callback.data, exc,
        )
        return await callback.answer("⚠️ битый callback", show_alert=True)

    event_id = int(cb.event_id)
    action = (cb.action or "").lower()
    logger.info(
        "event_action_callback: action=%s event_id=%s", action, event_id,
    )

    chat_id, err = await _resolve_event_chat_id(event_id)
    if err or not chat_id:
        logger.warning(
            "event_action_callback: cannot resolve chat_id for event_id=%s: %s",
            event_id, err,
        )
        return await callback.answer(err or "⚠️ ошибка", show_alert=True)

    if action == "reply":
        await state.set_state(ReplyState.waiting_text)
        await state.update_data(target_chat_id=chat_id)
        await callback.answer()
        await callback.message.answer(
            f"✍️ Введите сообщение для чата <code>{_escape(chat_id)}</code> "
            "(или пришлите фото/видео/документ).\n/cancel — выйти.",
            parse_mode="HTML",
        )
        # Помечаем чат как прочитанный в TG → MAX-процесс вызовет client.read_message.
        try:
            await api.mark_chat_read_up_to(chat_id=chat_id)
        except Exception as exc:
            logger.warning("mark_chat_read_up_to failed: %s", exc)
        logger.info(
            "event_action_callback: REPLY done, FSM set for chat_id=%s", chat_id,
        )
        return

    if action == "showid":
        await callback.answer(f"ID: {chat_id}", show_alert=True)
        # Помечаем чат как прочитанный.
        try:
            await api.mark_chat_read_up_to(chat_id=chat_id)
        except Exception as exc:
            logger.warning("mark_chat_read_up_to failed: %s", exc)
        logger.info(
            "event_action_callback: SHOWID done, chat_id=%s", chat_id,
        )
        return

    if action == "history":
        await callback.answer()
        # Помечаем чат как прочитанный.
        try:
            await api.mark_chat_read_up_to(chat_id=chat_id)
        except Exception as exc:
            logger.warning("mark_chat_read_up_to failed: %s", exc)
        logger.info(
            "event_action_callback: HISTORY loading for chat_id=%s", chat_id,
        )
        try:
            events = await api.list_events_for_chat(chat_id, limit=20)
        except Exception as exc:
            logger.error(
                "event_action_callback: list_events_for_chat failed: %s", exc,
            )
            await callback.message.answer(f"⚠️ Ошибка: {exc}")
            return
        if not events:
            await callback.message.answer("История пуста.")
            return
        for ev in events:
            try:
                await forward_event(callback.message.bot, callback.message.chat.id, ev)
            except Exception as exc:
                await callback.message.answer(
                    f"⚠️ Не удалось переслать {ev.get('id')}: {exc}"
                )
        return

    logger.warning(
        "event_action_callback: unknown action=%r (data=%r)",
        action, callback.data,
    )
    await callback.answer(f"⚠️ неизвестное действие: {action}", show_alert=True)


# ---------- Topic → MAX echo (без /reply) ----------


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
    """«Эхо» из топика супергруппы в MAX без команды /reply.

    Логика:
      * Если пользователь сейчас в ``ReplyState`` — пропускаем (FSM-хэндлер
        ``reply_text`` / ``reply_media`` обработает сам).
      * Если сообщение не в топике нашей супергруппы — игнорируем.
      * Если автор — бот (наша же пересылка из MAX) — игнорируем, иначе петля.
      * Если текст начинается с ``/`` — игнор (это команда, её обработают
        command-хэндлеры; aiogram и так не пропустит их сюда, но на всякий
        случай).
      * По ``(chat.id, message_thread_id)`` ищем ``ChatTopic`` в БД —
        он хранит ``max_chat_id``, в который и отправляем через
        ``api.enqueue_send``.
      * Скачиваем медиа в ``outbox`` и кладём запись в очередь.
      * Помечаем чат прочитанным в MAX (``mark_chat_read_up_to``).
      * Ставим ✅-реакцию на исходное сообщение как подтверждение.
    """
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
    try:
        if message.content_type == "text":
            res = await api.enqueue_send(
                target_chat_id=max_chat_id,
                kind="text",
                text=message.text or "",
                created_by=message.from_user.id,
                thread_id=int(thread_id),
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
            )
        logger.info(
            "topic_echo: enqueued send id=%s chat=%s thread=%s from uid=%s",
            res.get("id") if isinstance(res, dict) else res,
            max_chat_id, thread_id, message.from_user.id,
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


# Module-level реестр текущего ``AuthWatcher`` (один на процесс).
# Нужен, чтобы ``setgroup_command`` мог сбросить флаг подсказки
# «сделай /setup» при успешной привязке supergroup. Ключ — id,
# чтобы можно было держать несколько watcher'ов в тестах.
_active_auth_watcher: "dict[int, AuthWatcher]" = {}


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
        # Множество owner_uid, для которых УЖЕ отправляли подсказку
        # «есть undelivered, но supergroup не подключена — сделай /setup».
        # Чтобы не спамить на каждом тике (3 секунды). Сбрасывается,
        # когда владелец успешно делает /setgroup.
        self._supergroup_prompted_for: set[int] = set()

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

        # 6) Подсказка про /setup: если MAX авторизован (status=ok), есть
        #    непрочитанные события из MAX, но владелец не подключил
        #    supergroup для пересылки — напоминаем один раз. Чтобы не
        #    спамить на каждом тике, держим set owner_uid, которым уже
        #    отправили подсказку. Setgroup_command сбрасывает флаг.
        if status == "ok":
            undelivered = int(s.get("undelivered") or 0)
            if undelivered > 0:
                owner_uid = (
                    settings.allowed_tg_user_ids[0]
                    if settings.allowed_tg_user_ids else 0
                )
                if owner_uid and owner_uid not in self._supergroup_prompted_for:
                    try:
                        sg = shared_db.get_supergroup_for_owner(owner_uid)
                    except Exception as exc:
                        logger.debug(
                            "auth_watcher: get_supergroup_for_owner failed: %s", exc,
                        )
                        sg = None
                    if sg is None:
                        self._supergroup_prompted_for.add(owner_uid)
                        await self._notify_owner(
                            f"📬 У вас <b>{undelivered}</b> непрочитанных событий из MAX, "
                            "но бот пока не знает, в какую группу их пересылать.\n"
                            "Сделайте <code>/setup</code> — пришлю инструкцию "
                            "по созданию supergroup."
                        )
            else:
                # Нет непрочитанных — сбрасываем флаг, чтобы при следующем
                # всплеске событий снова напомнить.
                self._supergroup_prompted_for.clear()

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
            # Регистрируем себя в module-level реестре, чтобы
            # ``setgroup_command`` мог сбросить флаг подсказки.
            _active_auth_watcher[id(self)] = self

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        _active_auth_watcher.pop(id(self), None)

    @staticmethod
    def get_active() -> "Optional[AuthWatcher]":
        """Возвращает текущий активный watcher (один на процесс) или ``None``.

        Используется ``setgroup_command``, чтобы сбросить флаг «уже
        подсказали про /setup» после успешной привязки supergroup.
        """
        if not _active_auth_watcher:
            return None
        # Возвращаем последний запущенный (LIFO) — в проде он ровно один.
        return next(reversed(_active_auth_watcher.values()))


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
    dp.message.register(setup_command, Command("setup"))
    dp.message.register(setgroup_command, Command("setgroup"))
    dp.message.register(getlink_command, Command("getlink"))

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

    # Inline-кнопки под сообщениями из MAX (reply/showid/history) — единый
    # CallbackData-класс EventActionCallback с фабричным фильтром. Это
    # критично в aiogram 3.15, где смешивание F.callback_data.startswith(...)
    # с CallbackData.filter() ломает фильтрацию callback_query.
    dp.callback_query.register(event_action_callback, EventActionCallback.filter())
    dp.callback_query.register(
        session_use_callback, SessionUseCallback.filter()
    )
    dp.callback_query.register(
        sessions_refresh_callback, F.callback_data == "sessions_refresh"
    )
    dp.callback_query.register(
        auth_action_callback, AuthActionCallback.filter()
    )

    # «Эхо» из топика супергруппы в MAX. Регистрируем ПОСЛЕДНИМ, чтобы
    # он срабатывал только когда не сматчились ни команда, ни FSM-состояние.
    # Дополнительная проверка «наша ли это группа» — внутри самого хэндлера
    # (``topic_message_to_max``).
    dp.message.register(
        topic_message_to_max,
        F.func(lambda m: bool(getattr(getattr(m, "chat", None), "is_forum", False))),
    )
