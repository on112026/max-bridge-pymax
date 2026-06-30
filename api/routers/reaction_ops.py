"""FastAPI-роутер очереди реакций MAX ↔ Telegram.

Архитектура
-----------

Таблица ``reaction_ops_queue`` (см. ``shared/db/reaction_ops.py``)
объединяет три направления реакций через колонку ``direction``:

* ``"to_max"``        — задачи для MAX-процесса: TG-хэндлер
  ``MessageReactionUpdated`` кладёт сюда ``add`` / ``remove``, MAX-процесс
  применяет их через ``client.add_reaction`` / ``client.remove_reaction``.
  Плюс ``op="fetch_summary"`` (callback-кнопка «🔄 Реакции» в топике)
  — MAX-процесс делает свежий ``client.get_reactions`` и кладёт
  ``to_tg_summary``.

* ``"to_tg"``         — задачи для бота: MAX-процесс ловит
  ``on_reaction_update``, достаёт ``your_reaction`` владельца и кладёт
  сюда задачу. Бот ставит ботовскую ``setMessageReaction``.

* ``"to_tg_summary"`` — задачи для бота: MAX-процесс кладёт сюда
  ``summary_update`` со счётчиками. Бот создаёт/редактирует
  сообщение-сводку под входящим из MAX сообщением в топике
  (только для CHAT/CHANNEL).

Все эндпойнты защищены ``verify_api_key`` — как и остальные внутренние
маршруты моста.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from shared import db
from shared.api_auth import verify_api_key

from api.routers.reaction_ops_schemas import (
    ReactionOpEnqueueIn,
    ReactionOpEnqueueOut,
    ReactionOpFinishIn,
    ReactionOpFinishOut,
    ReactionOpList,
    ReactionOpOut,
    ReactionOpStatsOut,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_out(row) -> ReactionOpOut:
    """ORM-строка ``ReactionOpsQueue`` → ``ReactionOpOut``."""
    return ReactionOpOut(
        id=row.id,
        direction=row.direction,
        op=row.op,
        max_chat_id=row.max_chat_id,
        max_message_id=row.max_message_id,
        tg_chat_id=row.tg_chat_id,
        tg_thread_id=row.tg_thread_id,
        tg_message_id=row.tg_message_id,
        emoji=row.emoji,
        counters_json=row.counters_json,
        total_count=row.total_count,
        status=row.status,
        error=row.error,
        attempts=int(row.attempts or 0),
        created_at=row.created_at.isoformat() if row.created_at else None,
        started_at=row.started_at.isoformat() if row.started_at else None,
        finished_at=row.finished_at.isoformat() if row.finished_at else None,
    )


def _validate_direction(direction: str) -> str:
    if direction not in db.REACTION_OPS_VALID_DIRECTIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"direction must be one of "
                f"{db.REACTION_OPS_VALID_DIRECTIONS}, got {direction!r}"
            ),
        )
    return direction


# ---------------------------------------------------------------------------
# Enqueue (от бота или MAX-процесса)
# ---------------------------------------------------------------------------


@router.post(
    "/reaction_ops",
    response_model=ReactionOpEnqueueOut,
    dependencies=[Depends(verify_api_key)],
)
def post_reaction_op(item: ReactionOpEnqueueIn) -> ReactionOpEnqueueOut:
    """Положить задачу в очередь реакций.

    Инициатор — бот (``to_max``, реакция владельца в TG) или
    MAX-процесс (``to_tg``, ``to_tg_summary``, изменение реакций в MAX).
    """
    direction = _validate_direction(item.direction)
    if item.op not in db.REACTION_OPS_VALID_OPS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"op must be one of "
                f"{db.REACTION_OPS_VALID_OPS}, got {item.op!r}"
            ),
        )
    payload = item.model_dump()
    try:
        item_id = db.enqueue_reaction_op(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ReactionOpEnqueueOut(id=item_id, direction=direction, op=item.op)


# ---------------------------------------------------------------------------
# Claim / finish
# ---------------------------------------------------------------------------


@router.get(
    "/reaction_ops/next",
    response_model=Optional[ReactionOpOut],
    dependencies=[Depends(verify_api_key)],
)
def get_next_reaction_op(
    direction: str = Query(
        ..., description="to_max | to_tg | to_tg_summary",
    ),
) -> Optional[ReactionOpOut]:
    """MAX-процесс или бот забирает следующую ``pending``-задачу."""
    _validate_direction(direction)
    row = db.claim_next_reaction_op(direction)
    if not row:
        return None
    return _row_to_out(row)


@router.post(
    "/reaction_ops/{item_id}/finish",
    response_model=ReactionOpFinishOut,
    dependencies=[Depends(verify_api_key)],
)
def post_finish_reaction_op(
    item_id: int, body: ReactionOpFinishIn
) -> ReactionOpFinishOut:
    """Исполнитель сообщает о завершении задачи.

    ``body.ok=true`` → ``done``, иначе ``failed`` + ``body.error``.
    """
    db.finish_reaction_op(item_id, ok=body.ok, error=body.error)
    return ReactionOpFinishOut(ok=True)


# ---------------------------------------------------------------------------
# Polling / отладка
# ---------------------------------------------------------------------------


@router.get(
    "/reaction_ops/{item_id}",
    response_model=ReactionOpOut,
    dependencies=[Depends(verify_api_key)],
)
def get_reaction_op(item_id: int) -> ReactionOpOut:
    row = db.get_reaction_op(item_id)
    if not row:
        raise HTTPException(status_code=404, detail="reaction_op not found")
    return _row_to_out(row)


@router.get(
    "/reaction_ops/list",
    response_model=ReactionOpList,
    dependencies=[Depends(verify_api_key)],
)
def list_reaction_ops(
    direction: str = Query(..., description="to_max | to_tg | to_tg_summary"),
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> ReactionOpList:
    """Список ``pending``-задач направления (для отладки и UI).

    ``after_id`` — фильтр по ``id > after_id`` (для инкрементального
    polling'а со стороны бота).
    """
    _validate_direction(direction)
    rows = db.list_pending_reaction_ops(direction=direction, limit=limit)
    # Простая фильтрация по ``after_id`` уже после выборки (для маленьких
    # лимитов воркера достаточно). Если строк очень много — можно
    # добавить отдельный запрос с ``>``.
    if after_id:
        rows = [r for r in rows if int(r.id) > int(after_id)]
    return ReactionOpList(items=[_row_to_out(r) for r in rows])


@router.get(
    "/reaction_ops/stats",
    response_model=ReactionOpStatsOut,
    dependencies=[Depends(verify_api_key)],
)
def get_reaction_op_stats() -> ReactionOpStatsOut:
    """Статистика очереди для ``/status`` и мониторинга."""
    return ReactionOpStatsOut(**db.reaction_ops_queue_stats())