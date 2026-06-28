"""Inline-клавиатуры и CallbackData-классы для chat-операций MAX.

Этот модуль — «chat-ops-специфичная» часть ``app.keyboards``. Сюда
складываются :class:`aiogram.filters.callback_data.CallbackData`-фабрики
и генераторы :class:`aiogram.types.InlineKeyboardMarkup`, которые
используются только в хендлерах пакета :mod:`app.handlers.chat_ops`
(``/join`` / ``/resolve`` / ``/invite`` / ``/pending`` / ``/approve`` /
``/decline``).

Сейчас здесь только :class:`JoinChatCallback` для сценария
``/resolve <ссылка>``: после превью чата бот рисует inline-кнопки
«✅ Вступить» / «❌ Отмена». Callback ``action="join"`` повторно
вызывает :func:`app.handlers.chat_ops.join.do_join` с той же ссылкой,
``action="cancel"`` закрывает сообщение.

Если появятся новые chat-ops-кнопки (например, для ``/pending``
— карточка заявки с «✅ Approve» / «❌ Decline»), их CallbackData и
фабрики класть сюда же.

Префиксы :class:`CallbackData` выбираются так, чтобы не пересечься
с уже занятыми в :mod:`app.keyboards` (``auth`` / ``event`` /
``session_use`` / ``prune_topic``) и не путать диспатчер с invite-
callback'ами ``chat_op:invite_confirm:...`` / ``chat_op:invite_cancel``
(те парсятся вручную в :mod:`app.handlers.chat_ops.invite` и
используют обычный строковый ``callback_data`` без ``CallbackData``).
"""

from __future__ import annotations

from aiogram import types
from aiogram.filters.callback_data import CallbackData


class JoinChatCallback(CallbackData, prefix="chat_op_join"):
    """Inline-кнопки под превью чата из ``/resolve <ссылка>``.

    Поля:

    * ``action`` — ``"join"`` (нажали «✅ Вступить», повторно вызвать
      :func:`app.handlers.chat_ops.join.do_join` с этой же ``link``)
      или ``"cancel"`` («❌ Отмена», закрыть сообщение).
    * ``link`` — исходная ссылка, которую ввёл владелец (полная
      ``https://max.ru/join/<token>`` или просто ``join/<token>``).
      Хранится в callback-data, чтобы callback-хэндлеру не пришлось
      тащить состояние через FSM или доставать его из БД.
    """

    action: str   # "join" | "cancel"
    link: str     # исходная ссылка ``https://max.ru/join/<token>``


def join_chat_confirm_keyboard(link: str) -> types.InlineKeyboardMarkup:
    """Inline-клавиатура под превью чата: «✅ Вступить» / «❌ Отмена».

    Используется в :func:`app.handlers.chat_ops.join.do_resolve`
    сразу после успешного ``api.resolve_chat(link)``. Обе кнопки
    несут в ``callback_data`` исходный ``link`` через
    :class:`JoinChatCallback` — поэтому пользователь может нажать
    «Вступить» через несколько минут после ``/resolve``, и мы
    корректно отработаем со старой ссылкой.
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="✅ Вступить",
                    callback_data=JoinChatCallback(
                        action="join", link=link,
                    ).pack(),
                ),
                types.InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=JoinChatCallback(
                        action="cancel", link=link,
                    ).pack(),
                ),
            ],
        ]
    )