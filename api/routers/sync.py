"""Внутренние роутеры для синхронизации MAX → Telegram (вызываются max-процессом).

* ``POST /internal/sync_topics`` — max-процесс вызывает после успешного
  ``fetch_chats()``. Сравнивает свежий список MAX с уже существующими
  ``ChatTopic``, помечает пропавшие как ``stale=1`` и поставляет в очередь
  ``create``/``rename``-джобы для ``TopicSyncWorker``.
* ``POST /internal/notify`` — no-op, оставлен на будущее (push вместо polling).
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends

from shared import db
from shared.api_auth import verify_api_key

from api.routers.schemas import (
    NotifyIn,
    OkOut,
    SyncTopicsIn,
    SyncTopicsOut,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/internal/sync_topics",
    response_model=SyncTopicsOut,
    dependencies=[Depends(verify_api_key)],
)
def internal_sync_topics(body: SyncTopicsIn) -> SyncTopicsOut:
    """max-процесс вызывает после успешного ``fetch_chats()``.

    Логика:
      1. Сравнить ``body.chats`` с уже существующими ``ChatTopic`` по всем
         владельцам. Топики, чей ``max_chat_id`` НЕТ в свежем sync, пометить
         ``stale=1`` (MAX-чат пропал).
      2. Для каждой записи в ``super_groups`` (по каждому владельцу) —
         сматчить свежий список чатов с уже существующими топиками:
           * новый ``max_chat_id`` → джоб ``action="create"``;
           * ``title`` поменялся → джоб ``action="rename"``;
           * совпало — ничего не делаем, заодно сбрасываем stale=0.
      3. Возвращаем счётчики для логов max-процесса.

    Если владелец ещё не сделал ``/setgroup`` (``super_groups`` пуст) —
    возвращаем ``enqueued_jobs=0``, чаты просто сохранятся в ``chats``
    (это сделал эндпоинт ``POST /chats`` раньше в bridge.py).
    """
    trigger = (body.trigger or "").strip() or None
    chats = body.chats or []
    incoming_ids: set[str] = set()
    incoming_by_id: dict[str, dict] = {}
    for ch in chats:
        cid = str(ch.max_chat_id or "").strip()
        if not cid:
            continue
        incoming_ids.add(cid)
        incoming_by_id[cid] = {
            "max_chat_id": cid,
            "title": ch.title or "",
            "type": ch.type or "",
        }

    # 1) Помечаем stale для пропавших MAX-чатов (по всем владельцам).
    from sqlalchemy import select as _sel
    with db.session_scope() as s:
        existing_topic_ids = {
            row[0] for row in s.execute(_sel(db.ChatTopic.max_chat_id)).all()
        }
    missing_ids = [
        cid for cid in existing_topic_ids if cid not in incoming_ids
    ]
    stale_marked = db.mark_topics_stale(missing_ids) if missing_ids else 0

    # 2) Создаём задания на create/rename для каждого владельца.
    enqueued_total = 0
    by_action: dict = {"create": 0, "rename": 0}
    with db.session_scope() as s:
        owners = s.query(db.SuperGroup).all()
        s.expunge_all()
    for sg in owners:
        created = db.enqueue_topic_sync_jobs(
            owner_user_id=int(sg.owner_user_id),
            chats=list(incoming_by_id.values()),
            supergroup_chat_id=int(sg.supergroup_chat_id),
        )
        enqueued_total += len(created)

    # 2a) Одноразовая миграция имён топиков после изменения формата
    # ``(MAX: <id>)`` → ``(<label>: <id>)``. Делается ОДИН раз после
    # деплоя: для всех существующих топиков ставим ``rename``-джобы,
    # чтобы воркер перерисовал имена с учётом ``chat_type``. Флаг хранится
    # в ``system_state`` (``key="topics_v2_migrated"``), переживает рестарт.
    forced_total = 0
    with db.session_scope() as s:
        flag = s.get(db.SystemState, "topics_v2_migrated")
        already_migrated = bool(
            flag is not None and str(flag.value or "").strip() in ("1", "true")
        )
    if not already_migrated and owners:
        for sg in owners:
            forced = db.enqueue_topic_sync_jobs(
                owner_user_id=int(sg.owner_user_id),
                chats=list(incoming_by_id.values()),
                supergroup_chat_id=int(sg.supergroup_chat_id),
                force_rename=True,
            )
            forced_total += len(forced)
        if forced_total:
            with db.session_scope() as s:
                row = s.get(db.SystemState, "topics_v2_migrated")
                if row is None:
                    s.add(db.SystemState(key="topics_v2_migrated", value="1"))
                else:
                    row.value = "1"
                    row.updated_at = datetime.utcnow()
            logger.info(
                "internal_sync_topics: one-shot v2 migration enqueued "
                "%d rename jobs (chat_type labels)",
                forced_total,
            )
    enqueued_total += forced_total

    stats = db.get_topic_sync_stats()
    by_action["create"] = stats.get("pending_create", 0)
    by_action["rename"] = stats.get("pending_rename", 0)

    logger.info(
        "internal_sync_topics: trigger=%s incoming=%d missing=%d "
        "stale_marked=%d enqueued_jobs=%d stale_topics_total=%d",
        trigger, len(incoming_ids), len(missing_ids), stale_marked,
        enqueued_total, db.count_stale_topics(),
    )

    return SyncTopicsOut(
        trigger=trigger,
        synced_chats=len(incoming_ids),
        enqueued_jobs=enqueued_total,
        by_action=by_action,
        stale_topics=db.count_stale_topics(),
    )


@router.post("/internal/notify", dependencies=[Depends(verify_api_key)])
def internal_notify(body: NotifyIn) -> OkOut:
    """No-op, оставлен на будущее (push вместо polling)."""
    logger.info("internal_notify: %s %s", body.event, body.payload)
    return OkOut(ok=True)