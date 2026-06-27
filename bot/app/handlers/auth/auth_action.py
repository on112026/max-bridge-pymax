"""Inline-кнопки выбора способа авторизации MAX: ``sms`` / ``session`` / ``upload`` / ``cancel``.

Эти кнопки показывает ``AuthWatcher`` (см. ``app.handlers.auth_watcher``),
когда ``auth_state.status == auth_required`` или ``session_attached``.
Клик на кнопку превращается в callback ``auth:<action>``.

Действия:

* ``upload`` — переводит бота в FSM ``UploadSessionState.waiting_file`` и
  просит прислать файл сессии документом.
* ``sms`` / ``session`` / ``cancel`` — шлют ``POST /auth/action`` с
  соответствующим ``action``. Supervisor заберёт ``pending_action`` на
  следующей итерации.
"""

from __future__ import annotations

import logging

from aiogram import types
from aiogram.fsm.context import FSMContext

from app.api_client import api
from app.handlers._common import _is_allowed
from app.keyboards import AuthActionCallback
from app.states import ReauthSmsState, UploadSessionState

logger = logging.getLogger(__name__)


async def auth_action_callback(
        callback: types.CallbackQuery, state: FSMContext
) -> None:
    """Inline-кнопки выбора способа авторизации MAX.

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