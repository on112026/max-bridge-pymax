"""Регистрация всех хэндлеров бота в aiogram ``Dispatcher``.

Единственная публичная функция ``register_handlers(dp)`` собирает все
команды, inline-кнопки и FSM-обработчики из пакетов:

* ``basic`` — ``/start``, ``/help``, ``/status``, ``/chats``, ``/history``
  и reply-кнопки.
* ``supergroup`` — ``/setup``, ``/setgroup``, ``/autosetup``, ``/getlink``
  (см. ``app.handlers.supergroup.commands``).
* ``reply`` — ``/reply``, ``/cancel``, FSM ``reply_text`` / ``reply_media``.
* ``sessions`` — ``/upload_session``, FSM ``upload_session_file_handler``,
  ``/sessions``, ``session_use_callback``, ``sessions_refresh_callback``.
* ``auth`` — ``/reauth_sms``, ``/code``, ``event_action_callback``,
  ``auth_action_callback``, ``topic_message_to_max``.
* ``prune_topics`` — ``/prune_topics``, ``prune_topic_callback``.

Порядок регистрации важен: ``topic_message_to_max`` ставится **последним**
с ``F.func(lambda m: bool(getattr(getattr(m, "chat", None), "is_forum", False)))``
чтобы он срабатывал только когда не сматчились ни команда, ни FSM-состояние.
"""

from __future__ import annotations

from aiogram import Dispatcher, F

# Импорт из подмодулей нужен для регистрации, поэтому используем обычный import.
# ``auth`` и ``supergroup`` — теперь пакеты с несколькими модулями.
from app.handlers import auth  # noqa: F401  (reauth / event_callbacks / auth_action / topic_echo)
from app.handlers import basic, chat_ops, prune_topics, reply, sessions  # noqa: F401
from app.handlers import supergroup  # noqa: F401  (attach + commands)
from app.keyboards import (
    AuthActionCallback,
    EventActionCallback,
    PruneTopicCallback,
    SessionUseCallback,
)
from app.states import ReplyState, UploadSessionState


def register_handlers(dp: Dispatcher) -> None:
    """Зарегистрировать все хэндлеры в ``Dispatcher``.

    Порядок важен: сначала команды, потом FSM, потом inline-кнопки,
    в самом конце — ``topic_message_to_max`` как «catch-all» для топиков.
    """
    # ---------- Команды ----------
    dp.message.register(basic.start_command, F.text == "/start")
    dp.message.register(basic.help_command, F.text == "/help")
    dp.message.register(basic.status_command, F.text == "/status")
    dp.message.register(basic.chats_command, F.text == "/chats")
    dp.message.register(basic.history_command, F.text.startswith("/history "))
    dp.message.register(basic.history_command, F.text == "/history")  # без аргументов → подсказка

    dp.message.register(reply.reply_command, F.text.startswith("/reply "))
    dp.message.register(reply.reply_command, F.text == "/reply")
    dp.message.register(reply.cancel_command, F.text == "/cancel")

    dp.message.register(auth.reauth_sms_command, F.text == "/reauth_sms")
    dp.message.register(auth.code_command, F.text.startswith("/code "))
    dp.message.register(auth.code_command, F.text == "/code")

    dp.message.register(sessions.upload_session_command, F.text == "/upload_session")
    dp.message.register(sessions.sessions_command, F.text == "/sessions")

    dp.message.register(supergroup.setup_command, F.text == "/setup")
    dp.message.register(supergroup.setgroup_command, F.text.startswith("/setgroup "))
    dp.message.register(supergroup.setgroup_command, F.text == "/setgroup")
    dp.message.register(supergroup.autosetup_command, F.text == "/autosetup")
    dp.message.register(supergroup.getlink_command, F.text == "/getlink")

    dp.message.register(prune_topics.prune_topics_command, F.text == "/prune_topics")

    # ---------- Chat-операции MAX (join / invite / заявки / поиск) ----------
    chat_ops.register_handlers(dp)

    # ---------- Reply-кнопки (по точному тексту) ----------
    dp.message.register(basic.button_status, F.text == "ℹ️ Статус")
    dp.message.register(basic.button_chats, F.text == "📚 Чаты")
    dp.message.register(basic.button_help, F.text == "🆘 Помощь")
    dp.message.register(basic.button_listen, F.text == "📥 Слушать MAX")
    dp.message.register(
        sessions.button_upload_session, F.text == "📂 Загрузить сессию MAX"
    )
    dp.message.register(sessions.button_sessions, F.text == "📋 Сессии")

    # ---------- FSM: загрузка session-файла ----------
    dp.message.register(
        sessions.upload_session_file_handler,
        UploadSessionState.waiting_file,
        F.content_type == "document",
    )

    # ---------- FSM: reply (текст или медиа) ----------
    dp.message.register(
        reply.reply_text, ReplyState.waiting_text, F.content_type == "text"
    )
    dp.message.register(
        reply.reply_media,
        ReplyState.waiting_text,
        F.content_type.in_({"photo", "video", "document"}),
    )

    # ---------- Inline-кнопки ----------
    # Под сообщениями из MAX (reply/showid/history) — единый CallbackData.
    dp.callback_query.register(auth.event_action_callback, EventActionCallback.filter())

    # Выбор session-файла в /sessions.
    dp.callback_query.register(sessions.session_use_callback, SessionUseCallback.filter())
    dp.callback_query.register(
        sessions.sessions_refresh_callback, F.callback_data == "sessions_refresh"
    )

    # Inline-кнопки выбора способа авторизации MAX.
    dp.callback_query.register(auth.auth_action_callback, AuthActionCallback.filter())

    # Inline-кнопки /prune_topics (закрытие stale-топиков).
    dp.callback_query.register(prune_topics.prune_topic_callback, PruneTopicCallback.filter())

    # ---------- «Эхо» из топика супергруппы в MAX (catch-all) ----------
    # Регистрируем ПОСЛЕДНИМ, чтобы он срабатывал только когда не сматчились
    # ни команда, ни FSM-состояние. Дополнительная проверка «наша ли это
    # группа» — внутри самого хэндлера (``topic_message_to_max``).
    dp.message.register(
        auth.topic_message_to_max,
        F.func(lambda m: bool(getattr(getattr(m, "chat", None), "is_forum", False))),
    )