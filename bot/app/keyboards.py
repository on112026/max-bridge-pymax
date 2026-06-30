"""Клавиатуры Telegram-бота (этап 2, без headful)."""

from __future__ import annotations

from typing import Optional

from aiogram import types
from aiogram.filters.callback_data import CallbackData


class AuthActionCallback(CallbackData, prefix="auth"):
    """Inline-кнопки выбора способа авторизации MAX."""
    action: str  # "sms" | "session" | "cancel" | "upload"


class EventActionCallback(CallbackData, prefix="event"):
    """Inline-кнопки под сообщением из MAX.

    В aiogram 3.15 есть баг, когда фильтр ``F.callback_data.startswith(...)``
    не работает корректно, если в dispatcher одновременно есть хэндлеры
    с ``CallbackData.filter()``. Поэтому для inline-кнопок событий
    используем единый ``CallbackData``-класс с диспатчем по ``action``.
    """

    action: str  # "reply" | "showid" | "history"
    event_id: int


class SessionUseCallback(CallbackData, prefix="session_use"):
    """Callback для выбора конкретной сессии"""
    session_name: str


class PruneTopicCallback(CallbackData, prefix="prune_topic"):
    """Callback-фабрика для ``/prune_topics``.

    ``action``:
      * ``"close"`` — закрыть один конкретный топик (``max_chat_id``).
      * ``"close_all"`` — закрыть все stale-топики владельца.
    """

    action: str  # "close" | "close_all"
    max_chat_id: str = ""


class ReactionSummaryCallback(CallbackData, prefix="rx_sum"):
    """Inline-кнопка «🔄 Реакции» под сообщением из MAX в группе/канале.

    По клику бот перезапрашивает ``client.get_reactions`` в MAX,
    кладёт ``to_tg_summary``-задачу в ``reaction_ops_queue`` и
    воркер ``ReactionsMaxPoller`` обновит сводку «👍×N 🔥×M · итого K»
    под сообщением.

    ``max_chat_id`` хранится в callback_data (в отличие от ``event_id``,
    тут размер не критичен — MAX chat_id обычно 18-20 цифр).
    """

    max_chat_id: str
    max_message_id: str


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


def event_inline_keyboard(
    event_id: int,
    max_chat_id: str = "",
    chat_type: Optional[str] = None,
    max_message_id: Optional[str] = None,
) -> types.InlineKeyboardMarkup:
    """Inline-клавиатура под сообщением из MAX.

    Использует ``EventActionCallback`` (CallbackData-класс) — это критично
    в aiogram 3.15, где смешивание ``F.callback_data.startswith(...)`` с
    ``CallbackData.filter()`` ломает фильтрацию.

    Параметр ``max_chat_id`` оставлен в сигнатуре для совместимости с
    другими местами вызова и игнорируется (хэндлер достаёт ``max_chat_id``
    из БД через ``api.get_event(event_id)``).

    Параметр ``chat_type`` управляет кнопкой «🔄 Реакции»:

    * ``"CHAT"`` / ``"CHANNEL"`` (или ``"chat"`` / ``"channel"``) — добавляем
      кнопку, по клику бот перезапросит ``get_reactions`` и обновит сводку
      «👍×N 🔥×M · итого K» под сообщением в топике.
    * ``"DIALOG"`` (или ``None``, неизвестный тип) — кнопку не показываем,
      в ЛС достаточно зеркальной реакции бота (через ``MessageReactionUpdated``
      + ``setMessageReaction``).

    Параметр ``max_message_id`` нужен для ``ReactionSummaryCallback``;
    если не передан — кнопку не добавляем (старые пути вызова).
    """
    eid = int(event_id) if event_id else 0
    rows: list[list[types.InlineKeyboardButton]] = [
        [
            types.InlineKeyboardButton(
                text="💬 Ответить",
                callback_data=EventActionCallback(action="reply", event_id=eid).pack(),
            ),
            types.InlineKeyboardButton(
                text="📋 ID чата",
                callback_data=EventActionCallback(action="showid", event_id=eid).pack(),
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="🔄 История",
                callback_data=EventActionCallback(action="history", event_id=eid).pack(),
            ),
        ],
    ]
    # Кнопку реакций показываем только в группе/канале (там «чужие»
    # реакции отражаются в сводке). В ЛС — только зеркальная реакция.
    normalized_type = (chat_type or "").upper() if chat_type else ""
    if (
        normalized_type in ("CHAT", "CHANNEL")
        and max_chat_id
        and max_message_id
    ):
        rows.append(
            [
                types.InlineKeyboardButton(
                    text="🔄 Реакции",
                    callback_data=ReactionSummaryCallback(
                        max_chat_id=str(max_chat_id),
                        max_message_id=str(max_message_id),
                    ).pack(),
                ),
            ]
        )
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


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