"""Inline-кнопки под сообщениями из MAX: ``reply`` / ``showid`` / ``history``.

Использует единый ``EventActionCallback.filter()`` (фабричный фильтр
CallbackData-класса) — это критично в aiogram 3.15, где смешивание
``F.callback_data.startswith(...)`` с ``CallbackData.filter()``
ломает фильтрацию callback_query.

Действие (``reply`` / ``showid`` / ``history``) берётся из
распакованного callback_data.

``max_chat_id`` достаётся из БД через ``api.get_event(event_id)``,
потому что callback_data ограничен 64 байтами Telegram Bot API и
не вмещает полный ``max_chat_id`` (например, для 20-значного id).

После любого действия (``REPLY`` / ``SHOWID`` / ``HISTORY``) помечаем
чат как прочитанный в TG (``mark_chat_read_up_to``), чтобы MAX-процесс
синхронизировал ``client.read_message``.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from aiogram import types
from aiogram.fsm.context import FSMContext

from app.api_client import api
from app.handlers._common import _escape, _is_allowed
from app.keyboards import EventActionCallback
from app.sender import forward_event
from app.states import ReplyState

logger = logging.getLogger(__name__)


async def _resolve_event_chat_id(event_id: int) -> Tuple[Optional[str], Optional[types.Message]]:
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
        return None, None
    except Exception as exc:
        logger.error(
            "api.get_event(%s) FAILED: %s", event_id, exc, exc_info=True,
        )
        return None, None
    if not ev:
        return None, None
    chat_id = ev.get("max_chat_id") or ""
    if not chat_id:
        return None, None
    return chat_id, None


async def event_action_callback(
        callback: types.CallbackQuery, state: FSMContext
) -> None:
    """Inline-кнопки под сообщением из MAX: ``reply`` / ``showid`` / ``history``.

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

    chat_id, _err = await _resolve_event_chat_id(event_id)
    if not chat_id:
        return await callback.answer("⚠️ ошибка", show_alert=True)

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