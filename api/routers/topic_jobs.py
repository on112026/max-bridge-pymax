"""Роутер очереди задач синка топиков (для ``TopicSyncWorker`` в боте).

Эндпоинты:

* ``GET /topic_jobs/claim`` — воркер забирает пачку pending-джобов и
  переводит их в ``in_progress``.
* ``POST /topic_jobs/{id}/finish`` — воркер сообщает об успехе/ошибке
  после выполнения ``createForumTopic`` / ``editForumTopic``.
* ``GET /topic_jobs/stats`` — статистика по очереди (для ``/status``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from shared import db
from shared.api_auth import verify_api_key

from api.routers.schemas import (
    OkOut,
    TopicJobFinishIn,
    TopicJobList,
    TopicJobOut,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/topic_jobs/claim",
    response_model=TopicJobList,
    dependencies=[Depends(verify_api_key)],
)
def topic_jobs_claim(limit: int = Query(default=5, ge=1, le=50)) -> TopicJobList:
    """Bot-воркер раз в 2 секунды забирает пачку pending-джобов и
    превращает их в ``createForumTopic`` / ``editForumTopic``. Здесь только
    переводим в ``in_progress`` и возвращаем уже обновлённые строки.
    """
    rows = db.claim_pending_topic_jobs(limit=limit)
    return TopicJobList(
        jobs=[
            TopicJobOut(
                id=int(j.id),
                owner_user_id=int(j.owner_user_id),
                max_chat_id=str(j.max_chat_id),
                chat_title=j.chat_title,
                action=str(j.action),
                attempts=int(j.attempts or 0),
            )
            for j in rows
        ]
    )


@router.post(
    "/topic_jobs/{job_id}/finish",
    response_model=OkOut,
    dependencies=[Depends(verify_api_key)],
)
def topic_jobs_finish(job_id: int, body: TopicJobFinishIn) -> OkOut:
    """Bot-воркер сообщает об успехе/ошибке после выполнения джоба.

    Параллельно: при ``action="rename"`` и ``ok=True`` — обновляем
    ``ChatTopic.topic_name`` в БД, чтобы при следующем sync не было
    ложного «title поменялся».
    """
    db.finish_topic_sync_job(job_id, ok=body.ok, error=body.error)
    if body.ok:
        # Заодно синхронизируем ChatTopic.topic_name, если джоб был rename.
        try:
            with db.session_scope() as s:
                row = s.get(db.TopicSyncJob, job_id)
                if row is not None and row.action == "rename":
                    db.update_topic_name(
                        str(row.max_chat_id), (row.chat_title or "").strip()
                    )
        except Exception as exc:
            logger.warning(
                "topic_jobs_finish: update_topic_name for job %s failed: %s",
                job_id, exc,
            )
    return OkOut(ok=True)


@router.get(
    "/topic_jobs/stats",
    dependencies=[Depends(verify_api_key)],
)
def topic_jobs_stats() -> dict:
    """Сводка по очереди задач (для диагностики)."""
    return db.get_topic_sync_stats()