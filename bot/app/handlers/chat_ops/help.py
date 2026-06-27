"""``/chatops`` — справка по командам пакета ``chat_ops``.

Вынесена в отдельный модуль, чтобы при добавлении новых команд
(поиск по нику, поиск групп/каналов и т.п.) правился только один
файл — здесь.
"""

from __future__ import annotations

from aiogram import F, types

from app.handlers.chat_ops._common import _is_allowed, _reject


# Текст справки вынесен в константу, чтобы можно было показать его
# ещё где-нибудь (например, при ``/help``).
HELP_TEXT = (
    "🔧 Команды для управления чатами/пользователями MAX:\n\n"
    "/resolve <ссылка> — превью чата перед вступлением\n"
    "/join <ссылка> — вступить в группу/канал\n"
    "/search_user <+79…> — найти user_id по номеру телефона\n"
    "/invite <chat_id> <user_id> — пригласить в чат\n"
    "/invite <chat_id> <+79…> — найти по телефону и пригласить\n"
    "/pending <chat_id> — список заявок на вступление\n"
    "/approve <chat_id> <user_id> [...] — принять заявки\n"
    "/decline <chat_id> <user_id> [...] — отклонить заявки\n\n"
    "⚠️ Поиск по имени/нику в MAX сейчас не работает через pymax — "
    "только по номеру телефона. Чтобы вступить в группу по имени, "
    "сначала возьмите ссылку в самом MAX-клиенте (https://max.ru/join/…)."
)


async def chatops_help_command(message: types.Message) -> None:
    """``/chatops`` — список команд chat-операций."""
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    await message.answer(HELP_TEXT)


def register_handlers(dp) -> None:
    """Зарегистрировать хэндлер справки в ``dp``."""
    dp.message.register(chatops_help_command, F.text == "/chatops")