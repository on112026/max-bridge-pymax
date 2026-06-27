"""Роутер stale-топиков (для ``/prune_topics`` в боте).

Эндпоинты:

* ``GET /topics/stale`` — список ``stale=1`` топиков для конкретного
  владельца (бот показывает в ``/prune_topics``).
* ``POST /topics/{max_chat_id}/close`` — пометить топик закрытым
  (``stale=2``) после успешного ``closeForumTopic``.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from shared import db
from shared.api_auth import verify_api_key

from api.routers.schemas import (
    CloseTopicIn,
    OkOut,
    StaleTopicList,
    StaleTopicOut,
)

router = APIRouter()


@router.get(
    "/topics/stale",
    response_model=StaleTopicList,
    dependencies=[Depends(verify_api_key)],
)
def topics_stale(owner_user_id: int = Query(...)) -> StaleTopicList:
    """Список stale-топиков (``stale=1``) для конкретного владельца.

    Бот вызывает из команды ``/prune_topics``.
    """
    rows = db.list_stale_topics(int(owner_user_id))
    return StaleTopicList(
        topics=[
            StaleTopicOut(
                max_chat_id=str(t.max_chat_id),
                supergroup_chat_id=int(t.supergroup_chat_id),
                thread_id=int(t.thread_id),
                topic_name=t.topic_name,
            )
            for t in rows
        ]
    )


@router.post(
    "/topics/{max_chat_id}/close",
    response_model=OkOut,
    dependencies=[Depends(verify_api_key)],
)
def topics_close(max_chat_id: str, body: CloseTopicIn) -> OkOut:
    """Пометить топик закрытым. Проверяем, что топик принадлежит
    ``owner_user_id`` — иначе бот может закрыть чужой топик.
    """
    with db.session_scope() as s:
        sg = (
            s.query(db.SuperGroup)
            .filter(db.SuperGroup.owner_user_id == int(body.owner_user_id))
            .first()
        )
        if not sg:
            raise HTTPException(status_code=404, detail="owner has no supergroup")
        topic = (
            s.query(db.ChatTopic)
            .filter(
                db.ChatTopic.max_chat_id == str(max_chat_id),
                db.ChatTopic.supergroup_chat_id == int(sg.supergroup_chat_id),
            )
            .first()
        )
        if not topic:
            raise HTTPException(status_code=404, detail="topic not found")
        topic.stale = 2
        topic.updated_at = datetime.utcnow()
    return OkOut(ok=True)