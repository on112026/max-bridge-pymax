"""Команды ``/pending`` / ``/approve`` / ``/decline`` — заявки на вступление.

Логика:

* ``/pending <chat_id>`` — показать список заявок; в конце подсказка
  ``/approve <chat_id> 1 2 3`` для принятия всех.
* ``/approve <chat_id> <user_id> [...]`` — принять заявки.
* ``/decline <chat_id> <user_id> [...]`` — отклонить заявки.

Обе операции ``approve``/``decline`` идут через одну функцию
:func:`do_decide`, чтобы не дублировать polling-логику.
"""

from __future__ import annotations

import logging
from typing import List

from aiogram import F, types

from app.api_client import api
from app.handlers.chat_ops._common import (
    _escape,
    _is_allowed,
    _reject,
    format_join_requests,
    parse_user_ids,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Polling-хелпер
# ---------------------------------------------------------------------------


async def _wait_op_result(item_id: int, *, timeout: float):
    try:
        return await api.wait_chat_op(item_id, timeout=timeout)
    except Exception as exc:
        logger.warning("wait_chat_op failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# /pending
# ---------------------------------------------------------------------------


async def pending_command(message: types.Message) -> None:
    """``/pending <chat_id>`` — список заявок на вступление в чат MAX."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /pending <chat_id>")
        return
    chat_id = args[1].strip()

    try:
        enq = await api.list_join_requests(chat_id=chat_id)
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

    text, ids = format_join_requests(result_row.get("result"))
    extra = (
        f"\n\nПринять всех: /approve {chat_id} " + " ".join(str(x) for x in ids)
        if ids else ""
    )
    await message.answer(
        f"Заявки в <code>{_escape(chat_id)}</code>:\n{text}{extra}",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /approve / /decline
# ---------------------------------------------------------------------------


async def approve_command(message: types.Message) -> None:
    """``/approve <chat_id> <user_id> [...]`` — принять заявки."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    await do_decide(message, accept=True)


async def decline_command(message: types.Message) -> None:
    """``/decline <chat_id> <user_id> [...]`` — отклонить заявки."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    await do_decide(message, accept=False)


async def do_decide(message: types.Message, *, accept: bool) -> None:
    """Общая логика ``/approve`` и ``/decline``."""
    args = (message.text or "").split()
    if len(args) < 3:
        await message.answer(
            f"Использование: /{'approve' if accept else 'decline'} "
            f"<chat_id> <user_id> [...]"
        )
        return
    chat_id = args[1].strip()
    user_ids: List[int] = parse_user_ids(" ".join(args[2:])) or []
    if not user_ids:
        await message.answer("❌ Не удалось распознать user_id.")
        return

    try:
        if accept:
            enq = await api.confirm_join_requests(chat_id=chat_id, user_ids=user_ids)
        else:
            enq = await api.decline_join_requests(chat_id=chat_id, user_ids=user_ids)
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
        await message.answer(f"❌ <code>{_escape(err)}</code>", parse_mode="HTML")
        return

    verb = "Принял(а)" if accept else "Отклонил(а)"
    await message.answer(
        f"✅ {verb} заявки: " + ", ".join(f"<code>{x}</code>" for x in user_ids),
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Регистрация
# ---------------------------------------------------------------------------


def register_handlers(dp) -> None:
    """Зарегистрировать хэндлеры заявок в ``dp``."""
    dp.message.register(pending_command, F.text.startswith("/pending "))
    dp.message.register(pending_command, F.text == "/pending")
    dp.message.register(approve_command, F.text.startswith("/approve "))
    dp.message.register(approve_command, F.text == "/approve")
    dp.message.register(decline_command, F.text.startswith("/decline "))
    dp.message.register(decline_command, F.text == "/decline")