"""FSM-состояния Telegram-бота (этап 2, без headful)."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ReplyState(StatesGroup):
    waiting_text = State()
    waiting_media = State()


class ReauthSmsState(StatesGroup):
    """Состояние владельца после /reauth_sms: ждём /code <число>."""

    waiting_code = State()