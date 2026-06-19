"""Клавиатуры Telegram-бота (этап 2, без headful)."""

from __future__ import annotations

from aiogram import types
from aiogram.filters.callback_data import CallbackData


class AuthActionCallback(CallbackData, prefix="auth"):
    """Inline-кнопки выбора способа авторизации MAX.

    Префикс ``auth:`` чтобы не пересекаться с ``reply:`` / ``showid:`` /
    ``history:`` в общем пространстве callback_data.
    """

    action: str  # "sms" | "session" | "cancel" | "upload"


def main_reply_keyboard() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="📥 Слушать MAX")],
            [types.KeyboardButton(text="📚 Чаты"), types.KeyboardButton(text="ℹ️ Статус")],
            [
                types.KeyboardButton(text="🆘 Помощь"),
                types.KeyboardButton(text="🔐 /reauth_sms"),
            ],
            [types.KeyboardButton(text="📂 Загрузить сессию MAX")],
        ],
        resize_keyboard=True,
        selective=True,
    )


def event_inline_keyboard(event_id: int, max_chat_id: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="💬 Ответить", callback_data=f"reply:{max_chat_id}"
                ),
                types.InlineKeyboardButton(
                    text="📋 ID чата", callback_data=f"showid:{max_chat_id}"
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text="🔄 История", callback_data=f"history:{max_chat_id}"
                ),
            ],
        ]
    )


def auth_choice_keyboard(
    show_upload: bool = True,
    show_session_connect: bool = True,
) -> types.InlineKeyboardMarkup:
    """Inline-меню, которое AuthWatcher шлёт владельцу при status=auth_required.

    Кнопки:
      * 🔐 SMS-авторизация      → ``auth:sms``     (старт нового Client + SMS)
      * 📂 Подключиться по сессии → ``auth:session`` (только если есть файл)
      * 📎 Загрузить файл сессии  → ``auth:upload``  (только если файла ещё нет)
      * ⛔ Отмена                 → ``auth:cancel``
    """
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