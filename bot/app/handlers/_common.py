"""Общие хелперы для всех хэндлеров Telegram-бота.

Содержит:

* ``_is_allowed`` — проверка доступа (fail-closed, если
  ``ALLOWED_TG_USER_IDS`` пуст).
* ``_reject`` — стандартный ответ «⛔ Бот принимает сообщения только
  от авторизованных пользователей».
* ``_escape`` — минимальный HTML-escape для отправки сообщений в Telegram.
* ``_format_chat`` — компактное представление MAX-чата для ``/chats``.

Константы ``MAX_TG_DOWNLOAD`` и ``MAX_SESSION_SIZE`` — общие лимиты,
используются в нескольких хэндлерах (загрузка медиа и session-файлов).
"""

from __future__ import annotations

from typing import Any, Dict

from aiogram import types

from app.config import settings


# Лимит загрузки через Telegram Bot API — 50 МБ.
# Используем 49 МБ, чтобы был небольшой запас.
MAX_TG_DOWNLOAD = 49 * 1024 * 1024

# Лимит на размер session-файла MAX (PyMax session.db обычно < 1 МБ,
# но владелец может залить любой свой bridge.db — ограничиваем 50 МБ).
MAX_SESSION_SIZE = 50 * 1024 * 1024

# HTML escape-сущности. Собраны через ``chr()`` чтобы автоформаттеры IDE
# не «помогали» нам, превращая ``&`` обратно в ``&``.
_AMP = chr(38) + "amp;"
_LT = chr(38) + "lt;"
_GT = chr(38) + "gt;"


def _is_allowed(user_id: int) -> bool:
    """Проверка доступа: пустой ``ALLOWED_TG_USER_IDS`` → fail-closed."""
    if not settings.allowed_tg_user_ids:
        return False
    return user_id in settings.allowed_tg_user_ids


async def _reject(message: types.Message) -> None:
    """Стандартный ответ неавторизованному пользователю."""
    await message.answer(
        "⛔ Бот принимает сообщения только от авторизованных пользователей."
    )


def _escape(text: str) -> str:
    """Минимальный HTML-escape для Telegram (только ``&``, ``<``, ``>``)."""
    return (text or "").replace(chr(38), _AMP).replace(chr(60), _LT).replace(chr(62), _GT)


def _format_chat(chat: Dict[str, Any]) -> str:
    """Компактное представление MAX-чата для ``/chats``."""
    title = chat.get("title") or "—"
    cid = chat.get("max_chat_id")
    last = chat.get("last_message_preview") or ""
    return (
        f"<b>{_escape(title)}</b>\n"
        f"ID: <code>{cid}</code>\n"
        f"{_escape(last[:120])}"
    )