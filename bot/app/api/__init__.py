"""Пакет ``bot.app.api`` — клиент к внутреннему API моста (этап 2).

``BotApi`` собирается из ``core.BotApi`` + миксинов с методами по доменам.
Каждый миксин отвечает за один раздел API:

* ``events``   — ``list_undelivered`` / ``list_events_for_chat`` /
                 ``get_event`` / ``mark_delivered``.
* ``chats``    — ``list_chats`` / ``mark_chat_read_up_to`` /
                 ``get_pending_read_receipts``.
* ``send``     — ``enqueue_send`` / ``status``.
* ``auth``     — ``post_auth_state`` / ``put_2fa`` / ``request_2fa`` /
                 ``post_auth_action`` / ``consume_notify``.
* ``sessions`` — ``upload_session_file`` / ``list_sessions`` /
                 ``get_session_list`` / ``use_session``.
* ``topics``   — ``claim_topic_jobs`` / ``finish_topic_job`` /
                 ``topic_jobs_stats`` / ``list_stale_topics`` /
                 ``close_stale_topic``.
* ``chat_ops`` — ``join_chat`` / ``resolve_chat`` / ``invite_to_chat`` /
                 ``list_join_requests`` / ``confirm_join_requests`` /
                 ``decline_join_requests`` / ``search_user_by_phone`` /
                 ``wait_chat_op`` / ``get_chat_op`` / ``chat_op_stats``.

``bot/app/api_client.py`` — тонкая публичная обёртка, реэкспортирует
``BotApi`` для обратной совместимости со старым кодом (``from app.api_client import api``).
"""

from shared.http_client import ApiClient
from app.api.auth import AuthApiMixin
from app.api.chat_ops import ChatOpsApiMixin
from app.api.chats import ChatsApiMixin
from app.api.core import BotApi
from app.api.events import EventsApiMixin
from app.api.send import SendApiMixin
from app.api.sessions import SessionsApiMixin
from app.api.topics import TopicsApiMixin


class BotApiComposite(
    BotApi,
    EventsApiMixin,
    ChatsApiMixin,
    SendApiMixin,
    AuthApiMixin,
    SessionsApiMixin,
    TopicsApiMixin,
    ChatOpsApiMixin,
):
    """Финальный ``BotApi`` со всеми методами из миксинов.

    MRO собирает методы из всех миксинов в один класс. ``__init__`` —
    из ``BotApi`` (он первый в списке предков и не наследует ``__init__``
    от ``object``/миксинов; единственное место, где создаётся ``self._client``).
    """


__all__ = ["BotApi", "BotApiComposite"]