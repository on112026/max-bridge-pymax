"""Функции для работы с таблицей ``topic_sync_jobs`` — очередь задач синка топиков.

Max-процесс (в ``_on_start``) при auth=ok заливает сюда пачку задач
через ``POST /internal/sync_topics``. Bot-процесс (``TopicSyncWorker``)
раз в 2 секунды забирает pending-джобы через ``claim_pending_topic_jobs``
и через ``createForumTopic`` / ``editForumTopic`` создаёт/переименовывает
топики, помечая джоб done/failed (``finish_topic_sync_job``).

``action``:

* ``"create"`` — создать новый топик для ``max_chat_id``.
* ``"rename"`` — переименовать существующий топик.

``status``:

* ``"pending"``     — в очереди, ждёт воркера.
* ``"in_progress"`` — забрано воркером.
* ``"done"``        — успешно выполнено.
* ``"failed"``      — ошибка после ``MAX_ATTEMPTS`` попыток.

``attempts`` инкрементируется при каждом ``claim``; при ``attempts >= MAX_ATTEMPTS``
джоб помечается ``failed`` и больше не повторяется.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import List

from sqlalchemy import select, update

from shared.db._engine import session_scope
from shared.db._models import ChatTopic, TopicSyncJob


def enqueue_topic_sync_jobs(
    owner_user_id: int,
    chats: List[dict],
    supergroup_chat_id: int,
    force_rename: bool = False,
) -> List[int]:
    """Положить пачку задач ``create``/``rename`` в ``topic_sync_jobs``.

    ``chats`` — список ``{max_chat_id, title, type}`` (поле ``type``
    опционально — это тип чата из MAX: ``DIALOG`` / ``CHAT`` / ``CHANNEL``).
    Для каждого:

    * если топика ещё нет в ``chat_topics`` → ``action="create"``;
    * если топик есть и ``title`` поменялся → ``action="rename"``.

    ``force_rename=True`` — режим одноразовой миграции: для **всех**
    существующих топиков поставить ``action="rename"`` (даже если
    ``title`` не менялся). Используется из ``api/routers/sync.py`` после
    изменения формата имени топика (``(MAX: <id>)`` → ``(<label>: <id>)``),
    чтобы воркер перерисовал уже существующие топики. Вызывающий код
    обязан сам следить за тем, чтобы вызов был **один раз** (например,
    через флаг в ``system_state``), иначе на каждом ``auth_ok`` будут
    плодиться лишние джобы.

    Возвращает список id созданных джобов. Если у владельца ещё нет
    supergroup (``supergroup_chat_id is None``) — возвращает ``[]``.

    Дубль по ``(owner_user_id, max_chat_id, action, status='pending')``
    игнорируется: если для того же чата уже висит pending-джоб,
    новый не создаём.
    """
    if not chats or not supergroup_chat_id:
        return []
    out: List[int] = []
    with session_scope() as s:
        existing_topics = {
            t.max_chat_id: t
            for t in s.query(ChatTopic).filter(
                ChatTopic.supergroup_chat_id == int(supergroup_chat_id)
            ).all()
        }
        # Уже висящие pending-джобы — чтобы не плодить дубли.
        existing_jobs = set(
            (j.owner_user_id, j.max_chat_id, j.action)
            for j in s.query(TopicSyncJob).filter(
                TopicSyncJob.status.in_(("pending", "in_progress"))
            ).all()
        )
        for chat in chats:
            cid = str(chat.get("max_chat_id") or "")
            if not cid:
                continue
            title = (chat.get("title") or "").strip() or None
            chat_type = chat.get("type") or None
            if chat_type is not None:
                chat_type = str(chat_type).strip() or None
            existing_topic = existing_topics.get(cid)
            if existing_topic is None:
                action = "create"
            elif force_rename:
                # Разовая миграция: переименовать ВСЕ существующие топики,
                # чтобы воркер перерисовал имя с учётом chat_type.
                action = "rename"
            else:
                # Сравниваем сохранённое имя с новым (с trim'ом).
                old = (existing_topic.topic_name or "").strip()
                new = (title or "").strip()
                if old == new:
                    # Заодно сбрасываем stale, если был.
                    if existing_topic.stale == 1:
                        existing_topic.stale = 0
                        existing_topic.updated_at = datetime.utcnow()
                    continue
                action = "rename"
            key = (int(owner_user_id), cid, action)
            if key in existing_jobs:
                continue
            s.add(TopicSyncJob(
                owner_user_id=int(owner_user_id),
                max_chat_id=cid,
                chat_title=title,
                chat_type=chat_type,
                action=action,
                status="pending",
            ))
            existing_jobs.add(key)
            s.flush()
            out.append(int(s.query(TopicSyncJob).filter(
                TopicSyncJob.owner_user_id == int(owner_user_id),
                TopicSyncJob.max_chat_id == cid,
                TopicSyncJob.action == action,
                TopicSyncJob.status == "pending",
            ).order_by(TopicSyncJob.id.desc()).first().id))
    return out


def claim_pending_topic_jobs(limit: int = 5) -> List[TopicSyncJob]:
    """Атомарно забрать ``limit`` pending-джобов и пометить ``in_progress``.

    Увеличивает ``attempts``. Используется бот-воркером.
    """
    with session_scope() as s:
        rows = (
            s.execute(
                select(TopicSyncJob)
                .where(TopicSyncJob.status == "pending")
                .order_by(TopicSyncJob.created_at.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        if not rows:
            return []
        ids = [int(r.id) for r in rows]
        s.execute(
            update(TopicSyncJob)
            .where(TopicSyncJob.id.in_(ids))
            .values(
                status="in_progress",
                started_at=datetime.utcnow(),
                attempts=TopicSyncJob.attempts + 1,
            )
        )
        # Перечитываем, чтобы вернуть уже-обновлённые копии.
        rows = (
            s.execute(
                select(TopicSyncJob)
                .where(TopicSyncJob.id.in_(ids))
                .order_by(TopicSyncJob.id.asc())
            )
            .scalars()
            .all()
        )
        s.expunge_all()
        return list(rows)


def finish_topic_sync_job(
    job_id: int,
    ok: bool,
    error: str = None,
) -> None:
    """Пометить джоб ``done``/``failed``."""
    with session_scope() as s:
        row = s.get(TopicSyncJob, job_id)
        if not row:
            return
        row.status = "done" if ok else "failed"
        row.error = error
        row.finished_at = datetime.utcnow()


def count_pending_topic_jobs() -> int:
    """Сколько задач ещё ждёт в очереди (для ``/status``)."""
    with session_scope() as s:
        return int(
            s.query(TopicSyncJob)
            .filter(TopicSyncJob.status.in_(("pending", "in_progress")))
            .count()
            or 0
        )


def get_topic_sync_stats() -> dict:
    """Статистика по очереди задач на синк топиков."""
    with session_scope() as s:
        rows = (
            s.query(
                TopicSyncJob.status, TopicSyncJob.action
            ).all()
        )
        c = Counter((r.status, r.action) for r in rows)
        return {
            "pending_create": c.get(("pending", "create"), 0),
            "pending_rename": c.get(("pending", "rename"), 0),
            "in_progress_create": c.get(("in_progress", "create"), 0),
            "in_progress_rename": c.get(("in_progress", "rename"), 0),
            "done_create": c.get(("done", "create"), 0),
            "done_rename": c.get(("done", "rename"), 0),
            "failed_create": c.get(("failed", "create"), 0),
            "failed_rename": c.get(("failed", "rename"), 0),
        }