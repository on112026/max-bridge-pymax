"""Пакет роутеров FastAPI для моста MAX ↔ Telegram.

Структура (по доменам):

* ``health``     — ``GET /health``, ``GET /``.
* ``events``     — ``POST/GET /events``, ``GET /events/{id}``,
                   ``GET /events/by-chat/{chat_id}``, ``POST /events/{id}/delivered``.
* ``chats``      — ``POST/GET /chats``, ``/chats/{id}/read-up-to``,
                   ``/chats/pending-reads``, ``/chats/{id}/messages/{mid}/read``.
* ``send``       — ``POST /send``, ``GET /send/next``,
                   ``POST /send/{id}/finish``, ``GET /send/{id}``.
* ``status``     — ``GET /status``.
* ``auth``       — ``/auth/state``, ``/auth/2fa/*``, ``/auth/action``,
                   ``/auth/notify/consume``.
* ``sessions``   — ``/admin/session/upload``, ``/admin/session/list``,
                   ``/admin/session/use``.
* ``topic_jobs`` — ``/topic_jobs/claim``, ``/topic_jobs/{id}/finish``,
                   ``/topic_jobs/stats``.
* ``topics``     — ``/topics/stale``, ``/topics/{id}/close``.
* ``sync``       — ``/internal/sync_topics``, ``/internal/notify``.
* ``schemas``    — все Pydantic-модели.

``api/main.py`` собирает ``FastAPI(app)`` и подключает каждый ``router``
через ``include_router(...)``.
"""