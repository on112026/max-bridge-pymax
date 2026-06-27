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
from shared.db import auth_state  # noqa: F401
from shared.db import chats  # noqa: F401
from shared.db import events  # noqa: F401
from shared.db import read_receipts  # noqa: F401
from shared.db import send_queue  # noqa: F401
from shared.db import supergroups  # noqa: F401
from shared.db import topic_jobs  # noqa: F401
from shared.db import topics  # noqa: F401