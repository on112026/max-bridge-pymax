"""Команы ``/invite`` / ``/search_user`` — приглашение и поиск пользователей.

Логика:

* ``/search_user <+79…>`` — найти ``user_id`` по номеру телефона.
* ``/invite <chat_id> <user_id> [...]`` — пригласить по числовым id.
* ``/invite <chat_id> <+79…>`` — сначала :func:`search_user`, затем
  показать карточку с inline-кнопками «✅ Пригласить» / «❌ Отмена».

Вспомогательные :func:`do_invite` — общая точка выполнения: кладёт задачу
``invite`` в очередь, ждёт результат и форматирует ответ.
"""

from __future__ import annotations

import logging
from typing import List

from aiogram import F, types
from aiogram.fsm.context import FSMContext

from app.api_client import api
from app.handlers.chat_ops._common import (
    _escape,
    _is_allowed,
    _reject,
    format_user_result,
    parse_user_ids,
)
from app.states_chat_ops import InviteUserState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Polling-хелпер
# ---------------------------------------------------------------------------


async def _wait_op_result(item_id: int, *, timeout: float):
    """Дождаться завершения задачи ``chat_ops_queue``. ``None`` при ошибке сети."""
    try:
        return await api.wait_chat_op(item_id, timeout=timeout)
    except Exception as exc:
        logger.warning("wait_chat_op failed: %s", exc)
        return None


def _invite_confirm_keyboard(chat_id: str, user_id: int) -> types.InlineKeyboardMarkup:
    """Inline-кнопки под карточкой найденного пользователя."""
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="✅ Пригласить",
                    callback_data=f"chat_op:invite_confirm:{chat_id}:{user_id}",
                ),
                types.InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="chat_op:invite_cancel",
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


async def search_user_command(message: types.Message) -> None:
    """``/search_user <+79...>`` — найти user_id по номеру телефона."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Использование: /search_user <телефон>\n"
            "Пример: /search_user +79001234567"
        )
        return
    phone = args[1].strip()

    try:
        enq = await api.search_user_by_phone(phone=phone)
        item_id = int(enq.get("id") or 0)
        if not item_id:
            await message.answer("⚠️ API не вернул id задачи.")
            return
        result_row = await _wait_op_result(item_id, timeout=20.0)
    except Exception as exc:
        await message.answer(f"⚠️ Ошибка: {exc}")
        return

    if not result_row or result_row.get("status") != "done":
        err = (result_row or {}).get("error") or "неизвестная ошибка"
        await message.answer(f"❌ <code>{_escape(err)}</code>", parse_mode="HTML")
        return

    await message.answer(
        format_user_result(result_row.get("result")),
        parse_mode="HTML",
    )


async def invite_command(message: types.Message, state: FSMContext) -> None:
    """``/invite <chat_id> <user_id|телефон> [...]`` — пригласить в чат MAX."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    args = (message.text or "").split()
    if len(args) < 3:
        await message.answer(
            "Использование:\n"
            "  /invite <chat_id> <user_id> [user_id ...]\n"
            "  /invite <chat_id> <+79...>\n\n"
            "Пример:\n"
            "  /invite -1234567890123 987654321\n"
            "  /invite -1234567890123 +79001234567"
        )
        return
    chat_id = args[1].strip()
    target = args[2].strip()

    # Сценарий 1: сразу список user_id (числа).
    ids = parse_user_ids(target)
    if ids:
        all_ids: List[int] = list(ids)
        if len(args) > 3:
            extra = parse_user_ids(" ".join(args[3:]))
            if extra:
                all_ids.extend(extra)
        await do_invite(message, chat_id=chat_id, user_ids=all_ids)
        return

    # Сценарий 2: похоже на телефон — ищем через search_user.
    if target.startswith("+") and target[1:].isdigit():
        await state.set_state(InviteUserState.waiting_phone)
        await state.update_data(chat_id=chat_id, phone=target)
        try:
            enq = await api.search_user_by_phone(phone=target)
            item_id = int(enq.get("id") or 0)
            if not item_id:
                await message.answer("⚠️ API не вернул id задачи.")
                return
            result_row = await _wait_op_result(item_id, timeout=20.0)
        except Exception as exc:
            await message.answer(f"⚠️ Ошибка поиска: {exc}")
            await state.clear()
            return

        if not result_row or result_row.get("status") != "done" or not result_row.get("result"):
            err = (result_row or {}).get("error") or "пользователь не найден"
            await message.answer(f"❌ Не нашёл: <code>{_escape(err)}</code>", parse_mode="HTML")
            await state.clear()
            return

        user = result_row.get("result") or {}
        uid_int = None
        for k in ("id", "user_id"):
            try:
                uid_int = int(user.get(k))
                break
            except (TypeError, ValueError):
                continue
        if uid_int is None:
            await message.answer("❌ Поиск вернул пользователя без числового id.")
            await state.clear()
            return

        await state.update_data(user_id=uid_int)
        await message.answer(
            f"Нашёл: {format_user_result(user)}\n\n"
            "Пригласить этого пользователя в чат?",
            parse_mode="HTML",
            reply_markup=_invite_confirm_keyboard(chat_id=chat_id, user_id=uid_int),
        )
        return

    await message.answer(
        "❌ Аргумент должен быть user_id (число) или телефоном вида +79…"
    )


# ---------------------------------------------------------------------------
# Логика (вызывается из команды и из callback'а)
# ---------------------------------------------------------------------------


async def do_invite(message: types.Message, chat_id: str, user_ids: List[int]) -> None:
    """Отправить задачу invite и показать результат."""
    if not user_ids:
        await message.answer("❌ Не указаны user_id.")
        return
    try:
        enq = await api.invite_to_chat(chat_id=chat_id, user_ids=user_ids)
        item_id = int(enq.get("id") or 0)
        if not item_id:
            await message.answer("⚠️ API не вернул id задачи.")
            return
        result_row = await _wait_op_result(item_id, timeout=30.0)
    except Exception as exc:
        await message.answer(f"⚠️ Ошибка: {exc}")
        return

    if not result_row or result_row.get("status") != "done":
        err = (result_row or {}).get("error") or "неизвестная ошибка"
        await message.answer(
            f"❌ Приглашение не удалось: <code>{_escape(err)}</code>",
            parse_mode="HTML",
        )
        return

    result = result_row.get("result")
    if result is None:
        await message.answer("✅ Приглашение отправлено.")
        return
    if isinstance(result, list):
        if not result:
            await message.answer("✅ Приглашение отправлено.")
            return
        lines = []
        for item in result:
            if isinstance(item, dict):
                uid = item.get("user_id") or item.get("id")
                ok = item.get("ok")
                lines.append(f"  • <code>{_escape(str(uid))}</code>: {'✅' if ok else '❌'}")
            else:
                lines.append(f"  • <code>{_escape(str(item))}</code>")
        await message.answer(
            "Результат приглашения:\n" + "\n".join(lines),
            parse_mode="HTML",
        )
        return
    await message.answer(
        f"✅ Приглашение отправлено. Ответ: <code>{_escape(str(result))}</code>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Callback «Пригласить» / «Отмена» после search_user
# ---------------------------------------------------------------------------


async def invite_confirm_cb(query: types.CallbackQuery, state: FSMContext) -> None:
    """Inline-кнопки ``chat_op:invite_confirm:...`` / ``chat_op:invite_cancel``."""
    if not _is_allowed(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = (query.data or "").strip()
    if data == "chat_op:invite_cancel":
        await query.message.edit_text("🚫 Отменено.")
        await state.clear()
        await query.answer()
        return
    if data.startswith("chat_op:invite_confirm:"):
        parts = data.split(":")
        if len(parts) != 4:
            await query.answer("⚠️ Некорректный callback", show_alert=True)
            return
        chat_id = parts[2]
        try:
            user_id = int(parts[3])
        except ValueError:
            await query.answer("⚠️ Некорректный user_id", show_alert=True)
            return
        await query.message.edit_text("⏳ Приглашаю…")
        await state.clear()
        await query.answer()
        await do_invite(query.message, chat_id=chat_id, user_ids=[user_id])
        return
    await query.answer("⚠️ Неизвестное действие", show_alert=True)


# ---------------------------------------------------------------------------
# Регистрация
# ---------------------------------------------------------------------------


def register_handlers(dp) -> None:
    """Зарегистрировать хэндлеры приглашения в ``dp``."""
    dp.message.register(search_user_command, F.text.startswith("/search_user "))
    dp.message.register(search_user_command, F.text == "/search_user")
    dp.message.register(invite_command, F.text.startswith("/invite "))
    dp.message.register(invite_command, F.text == "/invite")

    # Inline-кнопки.
    dp.callback_query.register(
        invite_confirm_cb,
        F.callback_data.startswith("chat_op:invite_confirm:"),
    )
    dp.callback_query.register(
        invite_confirm_cb, F.callback_data == "chat_op:invite_cancel",
    )