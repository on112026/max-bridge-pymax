"""Функции для работы с таблицей ``reaction_ops_queue``.

Очередь операций над реакциями MAX ↔ Telegram. Три направления:

* ``"to_max"``         — задачи для MAX-процесса (``add_reaction`` /
                         ``remove_reaction`` через ``pymax.Client``).
                         Источник — Telegram-хэндлер реакций.
* ``"to_tg"``          — задачи для бота (``setMessageReaction`` в TG).
                         Источник — ``on_reaction_update`` в MAX-процессе.
* ``"to_tg_summary"``  — задачи для бота (обновить сообщение-сводку
                         по «чужим» реакциям в группе/канале).

Параметры операций и формат payload — см. docstring модели
:class:`shared.db._models.ReactionOpsQueue`.

Паттерн использования полностью повторяет ``chat_ops_queue``:

1. Инициатор (бот или MAX-процесс) кладёт задачу через
   :func:`enqueue_reaction_op` (``POST /reaction_ops``).
2. Исполнитель (MAX-процесс для ``to_max``, бот для ``to_tg`` /
   ``to_tg_summary``) забирает :func:`claim_next_reaction_op`
   (``GET /reaction_ops/next?direction=...``), переводит в ``in_progress``.
3. После выполнения — :func:`finish_reaction_op`
   (``POST /reaction_ops/{id}/finish``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import select

from shared.db._engine import session_scope
from shared.db._models import ReactionOpsQueue


VALID_DIRECTIONS = ("to_max", "to_tg", "to_tg_summary")
VALID_OPS = ("add", "remove", "summary_update", "fetch_summary")


def enqueue_reaction_op(item: dict) -> int:
    """Положить задачу в очередь реакций.

    Параметры ``item``:

    * ``direction``         — ``"to_max"`` / ``"to_tg"`` / ``"to_tg_summary"``.
    * ``op``                — ``"add"`` / ``"remove"`` / ``"summary_update"``.
    * ``max_chat_id``       — MAX chat_id (str), обязателен для ``to_max``,
                              опционален для остальных.
    * ``max_message_id``    — MAX message_id (str).
    * ``tg_chat_id``        — TG supergroup chat_id (int).
    * ``tg_thread_id``      — id топика.
    * ``tg_message_id``     — id TG-сообщения бота.
    * ``emoji``             — emoji-реакция (для ``add``/``remove``).
    * ``counters_json``     — JSON со счётчиками (для ``summary_update``).
    * ``total_count``       — суммарное число реакций (для ``summary_update``).

    Возвращает ``id`` созданной записи.
    """
    direction = (item.get("direction") or "").strip()
    op = (item.get("op") or "").strip()
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"invalid direction: {direction!r}")
    if op not in VALID_OPS:
        raise ValueError(f"invalid op: {op!r}")

    with session_scope() as s:
        row = ReactionOpsQueue(
            direction=direction,
            op=op,
            max_chat_id=(
                str(item["max_chat_id"]) if item.get("max_chat_id") is not None else None
            ),
            max_message_id=(
                str(item["max_message_id"]) if item.get("max_message_id") is not None else None
            ),
            tg_chat_id=_safe_int(item.get("tg_chat_id")),
            tg_thread_id=_safe_int(item.get("tg_thread_id")),
            tg_message_id=_safe_int(item.get("tg_message_id")),
            emoji=(item.get("emoji") or None),
            counters_json=item.get("counters_json"),
            total_count=_safe_int(item.get("total_count")),
            status="pending",
        )
        s.add(row)
        s.flush()
        return row.id


def claim_next_reaction_op(direction: str) -> Optional[ReactionOpsQueue]:
    """Атомарно взять следующую ``pending``-задачу нужного направления.

    ``direction`` — ``"to_max"`` / ``"to_tg"`` / ``"to_tg_summary"``.
    Возвращает ``None``, если задач нет. Задача помечается ``in_progress``,
    инкрементируется ``attempts``, проставляется ``started_at``.
    """
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"invalid direction: {direction!r}")
    with session_scope() as s:
        row = s.execute(
            select(ReactionOpsQueue)
            .where(
                ReactionOpsQueue.direction == direction,
                ReactionOpsQueue.status == "pending",
            )
            .order_by(ReactionOpsQueue.created_at.asc())
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


def finish_reaction_op(item_id: int, ok: bool, error: Optional[str] = None) -> None:
    """Пометить задачу ``done``/``failed`` после выполнения."""
    with session_scope() as s:
        row = s.get(ReactionOpsQueue, item_id)
        if not row:
            return
        row.status = "done" if ok else "failed"
        row.error = error
        row.finished_at = datetime.utcnow()


def get_reaction_op(item_id: int) -> Optional[ReactionOpsQueue]:
    """Прочитать задачу по ``id`` (для отладки)."""
    with session_scope() as s:
        row = s.get(ReactionOpsQueue, item_id)
        if not row:
            return None
        s.expunge(row)
        return row


def list_pending_reaction_ops(
    direction: str, limit: int = 50
) -> List[ReactionOpsQueue]:
    """Список ``pending``-задач нужного направления (для UI / отладки)."""
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"invalid direction: {direction!r}")
    with session_scope() as s:
        rows = (
            s.execute(
                select(ReactionOpsQueue)
                .where(
                    ReactionOpsQueue.direction == direction,
                    ReactionOpsQueue.status == "pending",
                )
                .order_by(ReactionOpsQueue.created_at.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        s.expunge_all()
        return list(rows)


def queue_stats() -> dict:
    """Статистика для ``/status`` и мониторинга."""
    with session_scope() as s:
        out: dict = {}
        for direction in VALID_DIRECTIONS:
            row_counts = {"pending": 0, "in_progress": 0, "done": 0, "failed": 0}
            for status in row_counts.keys():
                row_counts[status] = (
                    s.query(ReactionOpsQueue)
                    .filter(
                        ReactionOpsQueue.direction == direction,
                        ReactionOpsQueue.status == status,
                    )
                    .count()
                )
            out[direction] = row_counts
        return out


def _safe_int(value: Any) -> Optional[int]:
    """Аккуратное приведение к ``int`` (для JSON-полей из API)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None