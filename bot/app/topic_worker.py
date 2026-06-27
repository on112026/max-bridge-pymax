"""TopicSyncWorker — фоновая задача в bot-процессе.

Забирает pending-джобы из ``topic_sync_jobs`` (через API) и выполняет их
в Telegram:

* ``action="create"`` — ``createForumTopic`` через ``topics.get_or_create_topic``.
* ``action="rename"`` — ``editForumTopic`` через ``topics.rename_topic``.

После выполнения джоба (успех/ошибка) — вызывает ``api.finish_topic_job``.
При ``attempts >= MAX_ATTEMPTS`` и ошибке — помечает как ``failed``,
больше не повторяет.

Стартует из ``bot/run.py`` рядом с ``EventPoller`` и ``AuthWatcher``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from aiogram import Bot

from app.api_client import api
from app.topics import get_or_create_topic, rename_topic
from shared import db as shared_db

logger = logging.getLogger(__name__)


# Интервал опроса API. 2 секунды — компромисс между скоростью отклика
# (после auth=ok хочется сразу создать топики) и нагрузкой на api/sqlite.
POLL_INTERVAL = 2.0
# Сколько джобов забираем за один тик.
CLAIM_BATCH = 5
# После N ошибок перестаём ретраить (джоб помечается failed).
MAX_ATTEMPTS = 3


class TopicSyncWorker:
    """Асинхронная фоновая задача. ``start()/stop()`` управляют жизненным
    циклом из ``bot/run.py``. Для каждого ``owner_user_id`` ищется
    соответствующий ``super_groups.supergroup_chat_id`` в БД.
    """

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="topic-sync-worker")
        logger.info("TopicSyncWorker started (poll=%.1fs, batch=%d)", POLL_INTERVAL, CLAIM_BATCH)

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            except Exception:
                pass
            self._task = None
        logger.info("TopicSyncWorker stopped")

    async def _run(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("TopicSyncWorker tick error: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=POLL_INTERVAL
                )
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        """Один проход: забрать пачку джобов и выполнить их."""
        try:
            jobs = await api.claim_topic_jobs(limit=CLAIM_BATCH)
        except Exception as exc:
            logger.warning("TopicSyncWorker: claim failed: %s", exc)
            return
        if not jobs:
            return
        logger.info("TopicSyncWorker: claimed %d jobs", len(jobs))
        for job in jobs:
            if self._stop_event is not None and self._stop_event.is_set():
                break
            await self._process_job(job)

    async def _process_job(self, job: Dict[str, Any]) -> None:
        job_id = int(job.get("id") or 0)
        owner_user_id = int(job.get("owner_user_id") or 0)
        max_chat_id = str(job.get("max_chat_id") or "")
        action = str(job.get("action") or "")
        attempts = int(job.get("attempts") or 0)
        chat_title = job.get("chat_title")
        # Тип чата из MAX (DIALOG/CHAT/CHANNEL) — кладётся в джоб в
        # ``shared/db/topic_jobs.py::enqueue_topic_sync_jobs`` из
        # payload ``/internal/sync_topics``. Используется при формировании
        # имени топика, чтобы вместо ``(MAX: <id>)`` подставлять
        # ``(ЛС: <id>)`` / ``(группа: <id>)`` / ``(канал: <id>)``.
        chat_type = job.get("chat_type") or None
        if chat_type is not None:
            chat_type = str(chat_type).strip() or None

        if not job_id or not max_chat_id or action not in ("create", "rename"):
            await self._finish(job_id, ok=False, error="malformed job")
            return

        # Lookup supergroup для владельца (бот обслуживает одного владельца,
        # но кешируем «текущего» — см. ниже).
        sg = shared_db.get_supergroup_for_owner(owner_user_id)
        if sg is None:
            # Владелец ещё не сделал /setgroup — пропускаем, но НЕ помечаем
            # failed: если владелец позже привяжет группу, наш воркер
            # увидит джоб снова (attempts не вырастет, потому что мы
            # НЕ finish'им его).
            logger.info(
                "TopicSyncWorker: skip job %s (owner=%s has no supergroup yet)",
                job_id, owner_user_id,
            )
            # Возвращаем в pending — уменьшаем attempts, чтобы не
            # исчерпать лимит из-за того, что владелец ещё не настроил группу.
            try:
                with shared_db.session_scope() as s:
                    row = s.get(shared_db.TopicSyncJob, job_id)
                    if row is not None and row.status == "in_progress":
                        row.status = "pending"
                        row.started_at = None
            except Exception as exc:
                logger.warning("TopicSyncWorker: revert to pending failed: %s", exc)
            return

        supergroup_chat_id = int(sg.supergroup_chat_id)
        ok = False
        error_text: Optional[str] = None
        try:
            if action == "create":
                thread_id = await get_or_create_topic(
                    bot=self.bot,
                    supergroup_chat_id=supergroup_chat_id,
                    max_chat_id=max_chat_id,
                    chat_title=chat_title,
                    chat_type=chat_type,
                )
                ok = thread_id is not None
                if not ok:
                    error_text = "createForumTopic returned None (no admin / forum off?)"
            elif action == "rename":
                existing = shared_db.get_topic(max_chat_id)
                if existing is None:
                    # Топик исчез из БД между enqueue и worker'ом — это ОК,
                    # просто создадим новый.
                    thread_id = await get_or_create_topic(
                        bot=self.bot,
                        supergroup_chat_id=supergroup_chat_id,
                        max_chat_id=max_chat_id,
                        chat_title=chat_title,
                        chat_type=chat_type,
                    )
                    ok = thread_id is not None
                    if not ok:
                        error_text = "fallback createForumTopic returned None"
                else:
                    ok = await rename_topic(
                        bot=self.bot,
                        supergroup_chat_id=supergroup_chat_id,
                        thread_id=int(existing.thread_id),
                        max_chat_id=max_chat_id,
                        new_chat_title=chat_title,
                        chat_type=chat_type,
                    )
                    if not ok:
                        error_text = "editForumTopic failed"
        except Exception as exc:
            logger.warning(
                "TopicSyncWorker: job %s action=%s failed: %s",
                job_id, action, exc,
            )
            error_text = f"exception: {exc}"

        # Если исчерпали попытки — пометить failed; иначе success.
        if not ok and attempts < MAX_ATTEMPTS:
            logger.info(
                "TopicSyncWorker: job %s failed (attempt %d/%d), will retry",
                job_id, attempts, MAX_ATTEMPTS,
            )
            try:
                with shared_db.session_scope() as s:
                    row = s.get(shared_db.TopicSyncJob, job_id)
                    if row is not None and row.status == "in_progress":
                        row.status = "pending"
                        row.error = error_text
            except Exception as exc:
                logger.warning("TopicSyncWorker: revert to pending failed: %s", exc)
            return

        await self._finish(job_id, ok=ok, error=error_text)

    async def _finish(
        self,
        job_id: int,
        ok: bool,
        error: Optional[str],
    ) -> None:
        try:
            await api.finish_topic_job(job_id, ok=ok, error=error)
        except Exception as exc:
            logger.warning(
                "TopicSyncWorker: finish_topic_job %s failed: %s",
                job_id, exc,
            )
        if ok:
            logger.info("TopicSyncWorker: job %s done", job_id)
        else:
            logger.warning(
                "TopicSyncWorker: job %s marked failed (error=%s)",
                job_id, error,
            )