"""Клавиатуры Telegram-бота (этап 2, без headful)."""

from __future__ import annotations

from aiogram import types
from aiogram.filters.callback_data import CallbackData


class AuthActionCallback(CallbackData, prefix="auth"):
    """Inline-кнопки выбора способа авторизации MAX."""
    action: str  # "sms" | "session" | "cancel" | "upload"


class SessionUseCallback(CallbackData, prefix="session_use"):
    """Callback для выбора конкретной сессии"""
    session_name: str


def main_reply_keyboard() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="📥 Слушать MAX")],
            [types.KeyboardButton(text="📚 Чаты"), types.KeyboardButton(text="ℹ️ Статус")],
            [
                types.KeyboardButton(text="🆘 Помощь"),
                types.KeyboardButton(text="🔐 /reauth_sms"),
            ],
            [
                types.KeyboardButton(text="📋 Сессии"),
                types.KeyboardButton(text="📂 Загрузить сессию MAX"),
            ],
        ],
        resize_keyboard=True,
        selective=True,
    )


def event_inline_keyboard(event_id: int, max_chat_id: str = "") -> types.InlineKeyboardMarkup:
    """Inline-клавиатура под сообщением из MAX.

    В ``callback_data`` кладём **только короткий** ``event_id`` (число), а не
    ``max_chat_id``: PyMax возвращает длинные base64-подобные идентификаторы,
    которые вместе с префиксом легко превышают 64-байтный лимит Telegram Bot
    API на ``callback_data`` — тогда кнопка либо ломается, либо приходит с
    обрезанными данными и хэндлеры молча выходят (пользователь видит «часики»
    без реакции). Сам ``max_chat_id`` хэндлер достаёт из БД через
    ``api.get_event(event_id)``.

    Параметр ``max_chat_id`` оставлен в сигнатуре для совместимости с
    другими местами вызова и игнорируется.
    """
    eid = int(event_id) if event_id else 0
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="💬 Ответить", callback_data=f"reply:{eid}"
                ),
                types.InlineKeyboardButton(
                    text="📋 ID чата", callback_data=f"showid:{eid}"
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text="🔄 История", callback_data=f"history:{eid}"
                ),
            ],
        ]
    )


def auth_choice_keyboard(
        show_upload: bool = True,
        show_session_connect: bool = True,
) -> types.InlineKeyboardMarkup:
    """Inline-меню выбора способа авторизации."""
    rows: list[list[types.InlineKeyboardButton]] = []
    rows.append(
        [
            types.InlineKeyboardButton(
                text="🔐 SMS-авторизация",
                callback_data=AuthActionCallback(action="sms").pack(),
            )
        ]
    )
    if show_session_connect:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text="📂 Подключиться по сессии",
                    callback_data=AuthActionCallback(action="session").pack(),
                )
            ]
        )
    if show_upload:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text="📎 Загрузить файл сессии",
                    callback_data=AuthActionCallback(action="upload").pack(),
                )
            ]
        )
    rows.append(
        [
            types.InlineKeyboardButton(
                text="⛔ Отмена",
                callback_data=AuthActionCallback(action="cancel").pack(),
            )
        ]
    )
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def session_use_keyboard(session_names: list[str]) -> types.InlineKeyboardMarkup:
    """Клавиатура со списком доступных session-файлов для быстрого выбора."""
    rows: list[list[types.InlineKeyboardButton]] = []

    for name in session_names[:8]:  # не больше 8 кнопок за раз
        rows.append([
            types.InlineKeyboardButton(
                text=f"📄 {name}",
                callback_data=SessionUseCallback(session_name=name).pack(),
            )
        ])

    # Кнопка обновления списка
    rows.append([
        types.InlineKeyboardButton(
            text="🔄 Обновить список",
            callback_data="sessions_refresh"
        )
    ])

    return types.InlineKeyboardMarkup(inline_keyboard=rows)