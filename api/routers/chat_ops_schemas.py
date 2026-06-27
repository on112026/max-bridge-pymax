"""Pydantic-схемы для ``api/routers/chat_ops.py``.

Операции над чатами/пользователями MAX (join/invite/заявки/поиск) живут
в отдельном домене, чтобы не разрастался общий ``api/routers/schemas.py``.

Все операции идут через очередь ``chat_ops_queue`` (см.
``shared/db/chat_ops_queue.py``):

* ``POST /chat_ops/<op>``  — кладёт задачу в очередь и возвращает ``id``.
* ``GET /chat_ops/{id}``   — polling-ожидание результата (для синхронных
                              операций ``list_join_requests`` / ``search_user``).
* ``POST /chat_ops/{id}/finish`` — MAX-процесс сообщает о завершении
                              (``result`` или ``error``).
* ``GET /chat_ops/next``   — MAX-процесс забирает следующую задачу.

Списочные операции (с заявками) возвращают ``items`` как ``List[dict]`` —
pymax-объекты сериализуются через ``chat_ops._chat_to_dict`` /
``chat_ops._user_to_dict``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------- Базовые payload'ы операций (вход) ----------


class JoinChatIn(BaseModel):
    """Вход для ``POST /chat_ops/join`` — вступить в группу/канал.

    ``link`` — полная ссылка вида ``https://max.ru/join/<token>``
    (или просто ``join/<token>``). ``kind`` — подсказка роутеру, но
    фактический выбор делает pymax (через префикс ``join/``).
    """

    link: str = Field(..., min_length=1)
    kind: Optional[str] = None  # "group" | "channel" | None


class ResolveChatIn(BaseModel):
    """Вход для ``POST /chat_ops/resolve`` — превью чата без вступления."""

    link: str = Field(..., min_length=1)


class InviteUsersIn(BaseModel):
    """Вход для ``POST /chat_ops/invite`` — пригласить пользователей."""

    chat_id: str = Field(..., description="MAX chat_id (строка с числом)")
    user_ids: List[int] = Field(..., min_length=1)
    show_history: bool = True


class ListJoinRequestsIn(BaseModel):
    """Вход для ``POST /chat_ops/list_join_requests`` — список заявок."""

    chat_id: str


class JoinRequestDecisionIn(BaseModel):
    """Вход для ``confirm_join_request`` / ``decline_join_request``."""

    chat_id: str
    user_ids: List[int] = Field(..., min_length=1)


class SearchUserIn(BaseModel):
    """Вход для ``POST /chat_ops/search_user`` — поиск по телефону."""

    phone: str = Field(..., min_length=3, description="E.164, например '+79...'")


# ---------- Универсальный ответ на enqueue ----------


class ChatOpEnqueueOut(BaseModel):
    """Ответ ``POST /chat_ops/<op>``: задача положена в очередь."""

    id: int
    op: str
    status: str = "pending"
    created_at: Optional[str] = None


class ChatOpFinishIn(BaseModel):
    """Тело ``POST /chat_ops/{id}/finish`` от MAX-процесса."""

    ok: bool = True
    error: Optional[str] = None
    result: Optional[Any] = None


class ChatOpFinishOut(BaseModel):
    ok: bool = True


# ---------- Polling-ответ для синхронных операций ----------


class ChatOpOut(BaseModel):
    """Ответ ``GET /chat_ops/{id}`` — текущий статус задачи."""

    id: int
    op: str
    status: str
    error: Optional[str] = None
    result: Optional[Any] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    attempts: int = 0


class ChatOpStatsOut(BaseModel):
    """Ответ ``GET /chat_ops/stats`` — статистика очереди."""

    pending: int = 0
    in_progress: int = 0
    done: int = 0
    failed: int = 0


# ---------- Удобные схемы для результатов (чтобы OpenAPI был красивым) ----------


class ChatOpResultEnvelope(BaseModel):
    """Обёртка для ``result`` — на случай, если бот хочет ``op``-специфичную схему.

    Сам ``result`` остаётся ``Any``, потому что формат зависит от ``op``.
    """

    op: str
    result: Optional[Any] = None