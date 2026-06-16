"""Клавиатуры Telegram-бота (этап 2, без headful)."""

from __future__ import annotations

from aiogram import types


def main_reply_keyboard() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="📥 Слушать MAX")],
            [types.KeyboardButton(text="📚 Чаты"), types.KeyboardButton(text="ℹ️ Статус")],
            [types.KeyboardButton(text="🆘 Помощь"), types.KeyboardButton(text="🔐 /reauth_sms")],
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