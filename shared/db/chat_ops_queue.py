"""Функции для работы с таблицей ``chat_ops_queue``.

Жизненный цикл задачи:

1. Бот (или внешний код) кладёт задачу через :func:`enqueue_chat_op`
   (``POST /chat_ops/...`` в API).
2. MAX-процесс забирает :func:`claim_next_chat_op`
   (``GET /chat_ops/next``), переводит в ``in_progress`` и выполняет
   операцию через ``pymax.Client``.
3. MAX-процесс сообщает о результате через :func:`finish_chat_op`
   (``POST /chat_ops/{id}/finish``).

Для синхронных операций (``list_join_requests``, ``search_user``) результат
кладётся в ``result_json``, и API может сразу его прочитать (либо polling
через :func:`get_chat_op`, либо ожидание смены ``status`` в отдельном цикле —
в нашей реализации используется простой polling в API-слое).

Параметры операций и формат ``payload`` — см. docstring модели
:class:`shared.db._models.ChatOpsQueue`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select, update

from shared.db._engine import session_scope
from shared.db._models import ChatOpsQueue


def _serialize_payload(payload: Optional[dict]) -> str:
    """Сериализовать ``payload`` в JSON-строку (стабильный порядок ключей)."""
    if payload is None:
        return "{}"
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _deserialize_payload(raw: Optional[str]) -> dict:
    """Обратная операция: JSON → ``dict``. Не падает на пустых/некорректных строках."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def enqueue_chat_op(op: str, payload: Optional[dict] = None, created_by: Optional[int] = None) -> int:
    """Положить задачу в очередь chat-операций. Возвращает ``id`` созданной записи.

    ``op`` — тип операции (``"join"`` / ``"invite"`` / ``"resolve"`` /
    ``"list_join_requests"`` / ``"confirm_join_request"`` /
    ``"decline_join_request"`` / ``"search_user"``).
    """
    with session_scope() as s:
        row = ChatOpsQueue(
            op=op,
            payload=_serialize_payload(payload or {}),
            status="pending",
            created_by=created_by,
        )
        s.add(row)
        s.flush()
        return row.id


def claim_next_chat_op() -> Optional[ChatOpsQueue]:
    """Атомарно взять следующую pending-задачу и пометить ``in_progress``.

    Берём задачи в порядке ``created_at`` (FIFO). Если ничего нет — вернём
    ``None``. Уже взятая задача отдаётся в ``expunge``-состоянии, чтобы
    MAX-процесс мог спокойно работать с ней после закрытия сессии.
    """
    with session_scope() as s:
        row = s.execute(
            select(ChatOpsQueue)
            .where(ChatOpsQueue.status == "pending")
            .order_by(ChatOpsQueue.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        if not row:
            return None
        row.status = "in_progress"
        row.started_at = datetime.utcnow()
        row.attempts = (row.attempts or 0) + 1
        s.flush()
        s.expunge(row)
        return row


def finish_chat_op(
    item_id: int,
    ok: bool,
    error: Optional[str] = None,
    result: Optional[Any] = None,
) -> None:
    """Пометить задачу ``done``/``failed`` и опционально положить результат.

    ``result`` — произвольный JSON-сериализуемый объект (например,
    список заявок или найденный пользователь). Сериализуется в
    ``result_json``.
    """
    result_json: Optional[str] = None
    if result is not None:
        result_json = json.dumps(result, ensure_ascii=False, default=str)
    with session_scope() as s:
        s.execute(
            update(ChatOpsQueue)
            .where(ChatOpsQueue.id == item_id)
            .values(
                status="done" if ok else "failed",
                error=error,
                result_json=result_json,
                finished_at=datetime.utcnow(),
            )
        )


def get_chat_op(item_id: int) -> Optional[ChatOpsQueue]:
    """Прочитать задачу по ``id``. Полезно для polling-ожидания результата."""
    with session_scope() as s:
        row = s.get(ChatOpsQueue, item_id)
        if not row:
            return None
        s.expunge(row)
        return row


def queue_stats() -> dict:
    """Статистика по очереди для ``/status``."""
    with session_scope() as s:
        pending = s.query(ChatOpsQueue).filter(ChatOpsQueue.status == "pending").count()
        in_progress = s.query(ChatOpsQueue).filter(ChatOpsQueue.status == "in_progress").count()
        done = s.query(ChatOpsQueue).filter(ChatOpsQueue.status == "done").count()
        failed = s.query(ChatOpsQueue).filter(ChatOpsQueue.status == "failed").count()
        return {
            "pending": pending,
            "in_progress": in_progress,
            "done": done,
            "failed": failed,
        }


def requeue_failed(item_id: int) -> bool:
    """Перезапустить failed-задачу (для отладки). Возвращает ``True`` если переставлена."""
    with session_scope() as s:
        row = s.get(ChatOpsQueue, item_id)
        if not row or row.status != "failed":
            return False
        row.status = "pending"
        row.error = None
        row.finished_at = None
        return True


def payload_of(row: ChatOpsQueue) -> dict:
    """Утилита: декодировать ``payload`` ORM-строки в ``dict``."""
    return _deserialize_payload(row.payload)


def result_of(row: ChatOpsQueue) -> Any:
    """Утилита: декодировать ``result_json`` ORM-строки в произвольный объект."""
    if not row.result_json:
        return None
    try:
        return json.loads(row.result_json)
    except (TypeError, ValueError):
        return None