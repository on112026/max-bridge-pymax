"""Роутер ``/status`` — общий снимок состояния моста для ``AuthWatcher``.

Используется ``bot/app/handlers/auth_watcher.py`` (поллер раз в 3 секунды).
Возвращает:

* ``auth`` — словарь со всеми полями ``auth_state`` (см.
  ``shared.db.auth_state.get_auth_state``).
* ``queue`` — статистика по ``send_queue`` (pending/in_progress/sent/failed).
* ``undelivered`` — количество недоставленных событий MAX.
* ``chats`` — количество MAX-чатов в кэше.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from shared import db
from shared.api_auth import verify_api_key

from api.routers.schemas import StatusOut

router = APIRouter()


@router.get("/status", response_model=StatusOut, dependencies=[Depends(verify_api_key)])
def get_status() -> StatusOut:
    return StatusOut(
        auth=db.get_auth_state(),
        queue=db.queue_stats(),
        undelivered=len(db.list_undelivered_events(limit=1000)),
        chats=len(db.list_chats(limit=1000)),
    )