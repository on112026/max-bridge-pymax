"""Команды ``/join`` / ``/resolve`` — вступление в группу/канал MAX.

Логика:

* ``/resolve <ссылка>`` — превью чата через ``resolve_chat``, ответ с
  inline-кнопками «✅ Вступить» / «❌ Отмена».
* ``/join <ссылка>`` — сразу вступить через ``join_chat``.
* Callback :class:`JoinChatCallback` — обрабатывает нажатие «Вступить» /
  «Отмена» под превью: в первом случае вызывает :func:`do_join`, во втором
  закрывает сообщение.

Вспомогательные функции :func:`do_resolve` / :func:`do_join` принимают
``aiogram.types.Message`` (для ответов пользователю) и ``link`` —
используются и из команды, и из callback'а.
"""

from __future__ import annotations

import logging

from aiogram import F, types
from aiogram.fsm.context import FSMContext

from app.api_client import api
from app.handlers.chat_ops._common import (
    _escape,
    _is_allowed,
    _reject,
    format_chat_result,
)
from app.keyboards_chat_ops import JoinChatCallback, join_chat_confirm_keyboard

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Polling-хелпер
# ---------------------------------------------------------------------------


async def _wait_op_result(item_id: int, *, timeout: float):
    """Дождаться завершения задачи ``chat_ops_queue``. ``None`` при сетевой ошибке."""
    try:
        return await api.wait_chat_op(item_id, timeout=timeout)
    except Exception as exc:
        logger.warning("wait_chat_op failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


async def join_command(message: types.Message) -> None:
    """``/join <ссылка>`` — вступить в группу/канал MAX по ссылке."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Использование: /join <ссылка>\n"
            "Пример: /join https://max.ru/join/abcdef1234\n\n"
            "Сначала можно сделать /resolve <ссылка> для превью."
        )
        return
    await do_join(message, args[1].strip())


async def resolve_command(message: types.Message) -> None:
    """``/resolve <ссылка>`` — превью чата без вступления."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Использование: /resolve <ссылка>\n"
            "Пример: /resolve https://max.ru/join/abcdef1234"
        )
        return
    await do_resolve(message, args[1].strip())


# ---------------------------------------------------------------------------
# Логика (вызывается из команды и из callback'а)
# ---------------------------------------------------------------------------


async def do_resolve(message: types.Message, link: str) -> None:
    """Превью чата + кнопки «Вступить» / «Отмена»."""
    try:
        enq = await api.resolve_chat(link=link)
        item_id = int(enq.get("id") or 0)
        if not item_id:
            await message.answer("⚠️ API не вернул id задачи.")
            return
        result_row = await _wait_op_result(item_id, timeout=20.0)
    except Exception as exc:
        await message.answer(f"⚠️ Не удалось получить превью: {exc}")
        return

    if not result_row or result_row.get("status") != "done":
        err = (result_row or {}).get("error") or "неизвестная ошибка"
        await message.answer(
            f"❌ Не удалось получить превью: <code>{_escape(err)}</code>",
            parse_mode="HTML",
        )
        return

    chat_info = format_chat_result(result_row.get("result"))
    await message.answer(
        f"🔎 Превью чата:\n{chat_info}",
        parse_mode="HTML",
        reply_markup=join_chat_confirm_keyboard(link=link),
    )


async def do_join(message: types.Message, link: str) -> None:
    """Вступить в группу/канал. Используется и из ``/join``, и из callback'а."""
    try:
        enq = await api.join_chat(link=link)
        item_id = int(enq.get("id") or 0)
        if not item_id:
            await message.answer("⚠️ API не вернул id задачи.")
            return
        result_row = await _wait_op_result(item_id, timeout=30.0)
    except Exception as exc:
        await message.answer(f"⚠️ Не удалось вступить: {exc}")
        return

    if not result_row or result_row.get("status") != "done":
        err = (result_row or {}).get("error") or "неизвестная ошибка"
        await message.answer(
            f"❌ Не удалось вступить: <code>{_escape(err)}</code>",
            parse_mode="HTML",
        )
        return

    chat_info = format_chat_result(result_row.get("result"))
    await message.answer(f"✅ Вступил(а):\n{chat_info}", parse_mode="HTML")


# ---------------------------------------------------------------------------
# Callback «Вступить» / «Отмена» под превью
# ---------------------------------------------------------------------------


async def join_chat_callback(query: types.CallbackQuery, state: FSMContext) -> None:
    """Inline-кнопки :class:`JoinChatCallback`: «Вступить» / «Отмена»."""
    if not _is_allowed(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        data = JoinChatCallback.unpack(query.data or "")
    except Exception:
        await query.answer("⚠️ Некорректный callback", show_alert=True)
        return

    if data.action == "cancel":
        await query.message.edit_text("🚫 Отменено.")
        await query.answer()
        return

    if data.action == "join":
        await query.message.edit_text("⏳ Вступаю в чат…")
        await query.answer()
        await do_join(query.message, data.link)
        return

    await query.answer("⚠️ Неизвестное действие", show_alert=True)


# ---------------------------------------------------------------------------
# Регистрация хэндлеров модуля
# ---------------------------------------------------------------------------


def register_handlers(dp) -> None:
    """Зарегистрировать хэндлеры вступления в ``dp``."""
    # Команды.
    dp.message.register(join_command, F.text.startswith("/join "))
    dp.message.register(join_command, F.text == "/join")
    dp.message.register(resolve_command, F.text.startswith("/resolve "))
    dp.message.register(resolve_command, F.text == "/resolve")

    # Inline-кнопки.
    dp.callback_query.register(join_chat_callback, JoinChatCallback.filter())