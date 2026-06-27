"""Пакет ``shared.db`` — общая SQLite-БД моста MAX ↔ Telegram.

Структура (по доменам):

* ``_models``      — все ORM-модели (Event, Chat, SendQueue, AuthState,
                     SystemState, DeliveredMessage, ChatReadState,
                     SuperGroup, ChatTopic, TopicSyncJob).
* ``_engine``      — SQLAlchemy engine + ``init_engine`` + миграции
                     (``_apply_schema_migrations``) + ``session_scope``.
* ``events``       — ``upsert_event``, ``mark_event_delivered``,
                     ``list_undelivered_events``, ``list_events_for_chat``.
* ``chats``        — ``upsert_chat``, ``list_chats``.
* ``send_queue``   — ``enqueue_send``, ``claim_next_send``,
                     ``finish_send``, ``queue_stats``.
* ``auth_state``   — ``get_auth_state``, ``set_auth_state``,
                     ``set_pending_action``, ``consume_pending_action``,
                     ``set_notify_message``, ``consume_notify_message``,
                     ``set_session_file_path``,
                     ``open_2fa_request``, ``take_pending_2fa_code``,
                     ``put_2fa_code``, ``list_2fa_code_keys``,
                     ``clear_2fa_request``.
* ``read_receipts``— ``record_delivered``, ``update_chat_read_state``,
                     ``get_pending_read_receipts``, ``mark_delivered_as_read``.
* ``supergroups``  — ``get_supergroup_for_owner``, ``create_supergroup``,
                     ``update_supergroup_invite_link``.
* ``topics``       — ``get_topic``, ``get_topic_by_thread_id``,
                     ``create_topic``, ``update_topic_name``, ``list_topics``,
                     ``mark_topics_stale``, ``unmark_topic_stale``,
                     ``list_stale_topics``, ``mark_topic_closed``,
                     ``count_stale_topics``, ``count_topics_for_owner``.
* ``topic_jobs``   — ``enqueue_topic_sync_jobs``,
                     ``claim_pending_topic_jobs``, ``finish_topic_sync_job``,
                     ``count_pending_topic_jobs``, ``get_topic_sync_stats``.

Импорт через ``from shared import db`` (а также ``from shared.db import Event``
и т.д.) сохраняет обратную совместимость со старым ``shared/db.py``.
"""

# ---------- Engine / session_scope ----------
from shared.db._engine import (  # noqa: F401
    get_engine,
    init_engine,
    session_scope,
)

# ---------- ORM-модели ----------
from shared.db._models import (  # noqa: F401
    AuthState,
    Base,
    Chat,
    ChatReadState,
    ChatTopic,
    DeliveredMessage,
    Event,
    SendQueue,
    SuperGroup,
    SystemState,
    TopicSyncJob,
)

# ---------- Бизнес-функции (по доменам) ----------
# Импортируем функции напрямую в пространство имён ``db``, чтобы работал
# синтаксис ``db.get_auth_state()``, ``db.upsert_event()`` и т.д. — точно
# как в старом ``shared/db.py``. Просто ``import shared.db.auth_state``
# ввёл бы модуль-атрибут ``db.auth_state``, а не функции.

# events
from shared.db.events import (  # noqa: F401
    list_events_for_chat,
    list_undelivered_events,
    mark_event_delivered,
    upsert_event,
)

# chats
from shared.db.chats import (  # noqa: F401
    get_chat,
    list_chats,
    upsert_chat,
)

# send_queue
from shared.db.send_queue import (  # noqa: F401
    claim_next_send,
    enqueue_send,
    finish_send,
    queue_stats,
)

# auth_state
from shared.db.auth_state import (  # noqa: F401
    clear_2fa_request,
    consume_notify_message,
    consume_pending_action,
    get_auth_state,
    list_2fa_code_keys,
    open_2fa_request,
    put_2fa_code,
    set_auth_state,
    set_notify_message,
    set_pending_action,
    set_session_file_path,
    take_pending_2fa_code,
)

# read_receipts
from shared.db.read_receipts import (  # noqa: F401
    get_pending_read_receipts,
    mark_delivered_as_read,
    record_delivered,
    update_chat_read_state,
)

# supergroups
from shared.db.supergroups import (  # noqa: F401
    create_supergroup,
    get_supergroup_for_owner,
    update_supergroup_invite_link,
)

# topics
from shared.db.topics import (  # noqa: F401
    count_stale_topics,
    count_topics_for_owner,
    create_topic,
    get_topic,
    get_topic_by_thread_id,
    list_stale_topics,
    list_topics,
    mark_topic_closed,
    mark_topics_stale,
    unmark_topic_stale,
    update_topic_name,
)

# topic_jobs
from shared.db.topic_jobs import (  # noqa: F401
    claim_pending_topic_jobs,
    count_pending_topic_jobs,
    enqueue_topic_sync_jobs,
    finish_topic_sync_job,
    get_topic_sync_stats,
)

# chat_ops_queue — очередь операций над чатами/пользователями MAX
# (join/invite/заявки/поиск пользователя). Аналог ``send_queue``, только
# для админских операций через ``pymax.Client``. MAX-процесс
# (``chat_ops_loop``) забирает задачи polling'ом.
from shared.db.chat_ops_queue import (  # noqa: F401
    enqueue_chat_op,
    claim_next_chat_op,
    finish_chat_op,
    get_chat_op,
    queue_stats as chat_ops_queue_stats,
    requeue_failed as requeue_failed_chat_op,
    payload_of as chat_op_payload_of,
    result_of as chat_op_result_of,
)
