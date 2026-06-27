"""FastAPI-роутер chat-операций MAX: join / invite / заявки / поиск.

Архитектура
-----------

Этот роутер работает с таблицей ``chat_ops_queue``
(см. ``shared/db/chat_ops_queue.py``). Команды от бота попадают сюда
через ``POST /chat_ops/<op>``, кладутся в очередь как ``pending``;
MAX-процесс в фоне (``app.chat_ops.chat_ops_loop``) забирает их через
``GET /chat_ops/next``, выполняет через ``pymax.Client`` и сообщает
о результате через ``POST /chat_ops/{id}/finish``.

Синхронные операции (``list_join_requests`` / ``search_user``) возвращают
результат через ``result`` в ``POST /chat_ops/{id}/finish``, и бот
может его прочитать через polling ``GET /chat_ops/{id}``.

Все эндпойнты защищены ``verify_api_key`` — как и остальные внутренние
маршруты моста.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from shared import db
from shared.api_auth import verify_api_key

from api.routers.chat_ops_schemas import (
    ChatOpEnqueueOut,
    ChatOpFinishIn,
    ChatOpFinishOut,
    ChatOpOut,
    ChatOpStatsOut,
    InviteUsersIn,
    JoinChatIn,
    JoinRequestDecisionIn,
    ListJoinRequestsIn,
    ResolveChatIn,
    SearchUserIn,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_out(row) -> ChatOpOut:
    """ORM-строка ``ChatOpsQueue`` → ``ChatOpOut``."""
    return ChatOpOut(
        id=row.id,
        op=row.op,
        status=row.status,
        error=row.error,
        result=db.chat_op_result_of(row),
        created_at=row.created_at.isoformat() if row.created_at else None,
        started_at=row.started_at.isoformat() if row.started_at else None,
        finished_at=row.finished_at.isoformat() if row.finished_at else None,
        attempts=int(row.attempts or 0),
    )


def _enqueue(op: str, payload: dict, created_by: Optional[int] = None) -> ChatOpEnqueueOut:
    """Положить задачу в очередь, вернуть ``id`` и базовую инфу."""
    item_id = db.enqueue_chat_op(op=op, payload=payload, created_by=created_by)
    row = db.get_chat_op(item_id)
    if not row:
        raise HTTPException(status_code=500, detail="chat_op row missing after enqueue")
    return ChatOpEnqueueOut(
        id=row.id,
        op=row.op,
        status=row.status,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


# ---------------------------------------------------------------------------
# Enqueue (от бота)
# ---------------------------------------------------------------------------


@router.post(
    "/chat_ops/join",
    response_model=ChatOpEnqueueOut,
    dependencies=[Depends(verify_api_key)],
)
def post_join_chat(item: JoinChatIn, created_by: Optional[int] = None) -> ChatOpEnqueueOut:
    """Вступить в группу/канал MAX по ссылке."""
    return _enqueue("join", {"link": item.link, "kind": item.kind}, created_by=created_by)


@router.post(
    "/chat_ops/resolve",
    response_model=ChatOpEnqueueOut,
    dependencies=[Depends(verify_api_key)],
)
def post_resolve_chat(item: ResolveChatIn) -> ChatOpEnqueueOut:
    """Превью чата по ссылке (без вступления)."""
    return _enqueue("resolve", {"link": item.link})


@router.post(
    "/chat_ops/invite",
    response_model=ChatOpEnqueueOut,
    dependencies=[Depends(verify_api_key)],
)
def post_invite_users(item: InviteUsersIn) -> ChatOpEnqueueOut:
    """Пригласить пользователей в чат MAX."""
    return _enqueue(
        "invite",
        {
            "chat_id": item.chat_id,
            "user_ids": [int(x) for x in item.user_ids],
            "show_history": bool(item.show_history),
        },
    )


@router.post(
    "/chat_ops/list_join_requests",
    response_model=ChatOpEnqueueOut,
    dependencies=[Depends(verify_api_key)],
)
def post_list_join_requests(item: ListJoinRequestsIn) -> ChatOpEnqueueOut:
    """Получить список заявок на вступление."""
    return _enqueue("list_join_requests", {"chat_id": item.chat_id})


@router.post(
    "/chat_ops/confirm_join_request",
    response_model=ChatOpEnqueueOut,
    dependencies=[Depends(verify_api_key)],
)
def post_confirm_join_requests(item: JoinRequestDecisionIn) -> ChatOpEnqueueOut:
    """Принять одну или несколько заявок на вступление."""
    return _enqueue(
        "confirm_join_request",
        {
            "chat_id": item.chat_id,
            "user_ids": [int(x) for x in item.user_ids],
        },
    )


@router.post(
    "/chat_ops/decline_join_request",
    response_model=ChatOpEnqueueOut,
    dependencies=[Depends(verify_api_key)],
)
def post_decline_join_requests(item: JoinRequestDecisionIn) -> ChatOpEnqueueOut:
    """Отклонить одну или несколько заявок на вступление."""
    return _enqueue(
        "decline_join_request",
        {
            "chat_id": item.chat_id,
            "user_ids": [int(x) for x in item.user_ids],
        },
    )


@router.post(
    "/chat_ops/search_user",
    response_model=ChatOpEnqueueOut,
    dependencies=[Depends(verify_api_key)],
)
def post_search_user(item: SearchUserIn) -> ChatOpEnqueueOut:
    """Найти пользователя по номеру телефона (``pymax.Client.search_by_phone``)."""
    return _enqueue("search_user", {"phone": item.phone})


# ---------------------------------------------------------------------------
# MAX-процесс: claim / finish
# ---------------------------------------------------------------------------


@router.get(
    "/chat_ops/next",
    response_model=Optional[ChatOpOut],
    dependencies=[Depends(verify_api_key)],
)
def get_next_chat_op() -> Optional[ChatOpOut]:
    """MAX-процесс забирает следующую ``pending``-задачу (атомарно)."""
    row = db.claim_next_chat_op()
    if not row:
        return None
    return _row_to_out(row)


@router.post(
    "/chat_ops/{item_id}/finish",
    response_model=ChatOpFinishOut,
    dependencies=[Depends(verify_api_key)],
)
def post_finish_chat_op(item_id: int, body: ChatOpFinishIn) -> ChatOpFinishOut:
    """MAX-процесс сообщает о завершении задачи.

    ``body.ok=false`` → помечаем ``failed`` + ``error``.
    ``body.ok=true``  → помечаем ``done``   + ``body.result`` (если есть).
    """
    db.finish_chat_op(item_id, ok=body.ok, error=body.error, result=body.result)
    return ChatOpFinishOut(ok=True)


# ---------------------------------------------------------------------------
# Polling (от бота)
# ---------------------------------------------------------------------------


@router.get(
    "/chat_ops/{item_id}",
    response_model=ChatOpOut,
    dependencies=[Depends(verify_api_key)],
)
def get_chat_op(
    item_id: int,
    wait: bool = Query(default=False, description="Ждать завершения (до timeout секунд)"),
    timeout: float = Query(default=30.0, ge=0.0, le=120.0),
    poll_interval: float = Query(default=0.5, ge=0.05, le=5.0),
) -> ChatOpOut:
    """Текущий статус задачи.

    Если ``wait=true`` — крутимся в polling до ``status in (done, failed)``
    или до истечения ``timeout``. Это позволяет боту получить результат
    синхронных операций (``search_user``, ``list_join_requests``) одним
    запросом, без отдельного polling-цикла в боте.
    """
    deadline = time.monotonic() + timeout
    while True:
        row = db.get_chat_op(item_id)
        if not row:
            raise HTTPException(status_code=404, detail="chat_op not found")
        if row.status in ("done", "failed"):
            return _row_to_out(row)
        if not wait or time.monotonic() >= deadline:
            return _row_to_out(row)
        time.sleep(poll_interval)


@router.get(
    "/chat_ops/stats",
    response_model=ChatOpStatsOut,
    dependencies=[Depends(verify_api_key)],
)
def get_chat_op_stats() -> ChatOpStatsOut:
    """Статистика очереди для ``/status``."""
    s = db.chat_ops_queue_stats()
    return ChatOpStatsOut(**s)


@router.post(
    "/chat_ops/{item_id}/requeue",
    response_model=ChatOpFinishOut,
    dependencies=[Depends(verify_api_key)],
)
def post_requeue_chat_op(item_id: int) -> ChatOpFinishOut:
    """Переставить ``failed``-задачу в ``pending`` (для отладки/ручного retry)."""
    ok = db.requeue_failed_chat_op(item_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="chat_op is not in 'failed' status or not found",
        )
    return ChatOpFinishOut(ok=True)