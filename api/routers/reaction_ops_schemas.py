"""Pydantic-схемы для ``api/routers/reaction_ops.py``.

Эндпоинты для очереди реакций MAX ↔ Telegram:

* ``POST /reaction_ops`` — положить задачу (``to_max`` / ``to_tg`` /
  ``to_tg_summary``). Инициатор — бот или MAX-процесс.
* ``GET /reaction_ops/next?direction=...`` — забрать ``pending``-задачу
  нужного направления. Исполнитель — MAX-процесс (``to_max``) или бот
  (``to_tg`` / ``to_tg_summary``).
* ``POST /reaction_ops/{id}/finish`` — пометить задачу ``done``/``failed``.
* ``GET /reaction_ops/stats`` — статистика очереди.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class ReactionOpEnqueueIn(BaseModel):
    """Тело ``POST /reaction_ops``.

    Все поля опциональны, но должны соответствовать направлению и ``op``
    (валидация частично продублирована в ``shared.db.reaction_ops.enqueue_reaction_op``).
    """

    direction: str = Field(..., description="to_max | to_tg | to_tg_summary")
    op: str = Field(..., description="add | remove | summary_update | fetch_summary")
    max_chat_id: Optional[str] = None
    max_message_id: Optional[str] = None
    tg_chat_id: Optional[int] = None
    tg_thread_id: Optional[int] = None
    tg_message_id: Optional[int] = None
    emoji: Optional[str] = None
    counters_json: Optional[str] = None
    total_count: Optional[int] = None


class ReactionOpEnqueueOut(BaseModel):
    id: int
    direction: str
    op: str
    status: str = "pending"


class ReactionOpOut(BaseModel):
    """Ответ ``GET /reaction_ops/next`` и ``GET /reaction_ops/{id}``."""

    id: int
    direction: str
    op: str
    max_chat_id: Optional[str] = None
    max_message_id: Optional[str] = None
    tg_chat_id: Optional[int] = None
    tg_thread_id: Optional[int] = None
    tg_message_id: Optional[int] = None
    emoji: Optional[str] = None
    counters_json: Optional[str] = None
    total_count: Optional[int] = None
    status: str
    error: Optional[str] = None
    attempts: int = 0
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class ReactionOpFinishIn(BaseModel):
    ok: bool = True
    error: Optional[str] = None


class ReactionOpFinishOut(BaseModel):
    ok: bool = True


class ReactionOpStatsOut(BaseModel):
    """Счётчики по направлениям и статусам (для ``/status``)."""

    to_max: dict = Field(default_factory=dict)
    to_tg: dict = Field(default_factory=dict)
    to_tg_summary: dict = Field(default_factory=dict)


class ReactionOpList(BaseModel):
    items: List[ReactionOpOut]