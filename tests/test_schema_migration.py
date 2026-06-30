"""Smoke-test: эмулируем «старую» БД без новых колонок ``auth_state``.

Создаём файл SQLite руками с устаревшим DDL (только ``id`` и ``status``),
прогоняем ``shared.db.init_engine`` и проверяем, что:

1. недостающие колонки добавлены через ``ALTER TABLE``;
2. ``SELECT auth_state.*`` отрабатывает без ``OperationalError``;
3. повторный запуск ``init_engine`` идемпотентен.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# Подключаем /app/shared к sys.path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "shared"))

from sqlalchemy import text  # noqa: E402

from shared import db  # noqa: E402


LEGACY_AUTH_STATE_DDL = """
CREATE TABLE auth_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status VARCHAR DEFAULT 'unknown'
)
"""

LEGACY_SYSTEM_STATE_DDL = """
CREATE TABLE system_state (
    key VARCHAR PRIMARY KEY,
    value TEXT
)
"""

# Старая схема ``topic_sync_jobs`` без колонки ``chat_type`` (добавлена
# позже для передачи типа чата из MAX в бот — см. ``shared/db/_models.py``).
LEGACY_TOPIC_SYNC_JOBS_DDL = """
CREATE TABLE topic_sync_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    max_chat_id VARCHAR NOT NULL,
    chat_title VARCHAR,
    action VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at DATETIME,
    started_at DATETIME,
    finished_at DATETIME,
    attempts INTEGER NOT NULL DEFAULT 0
)
"""


def _column_names(cur: sqlite3.Cursor, table: str) -> set[str]:
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "bridge.db")
        # Шаг 1: создаём руками "старую" БД без новых колонок auth_state и без updated_at в system_state.
        con = sqlite3.connect(db_path)
        try:
            con.executescript(LEGACY_AUTH_STATE_DDL)
            con.executescript(LEGACY_SYSTEM_STATE_DDL)
            con.commit()
        finally:
            con.close()

        # Проверяем, что в старой БД действительно нет новых колонок.
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        legacy_cols = _column_names(cur, "auth_state")
        assert "pending_2fa_kind" not in legacy_cols, legacy_cols
        legacy_sys = _column_names(cur, "system_state")
        assert "updated_at" not in legacy_sys, legacy_sys
        con.close()

        # Шаг 2: запускаем init_engine — он должен применить авто-миграцию.
        db.init_engine(db_path)

        engine = db.get_engine()
        with engine.connect() as conn:
            rows = list(conn.execute(text("SELECT * FROM auth_state")))
            assert rows, "ожидалась единственная AuthState(status='unknown')"
            # Если SELECT * прошёл без ошибки — значит, колонки на месте.

        # Шаг 3: проверяем, что новые колонки действительно появились.
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cols = _column_names(cur, "auth_state")
        for required in (
            "pending_2fa_request_id",
            "pending_2fa_kind",
            "last_2fa_request_at",
            "last_login_at",
            "last_error",
            "updated_at",
        ):
            assert required in cols, f"auth_state не содержит {required}, есть {cols}"
        sys_cols = _column_names(cur, "system_state")
        assert "updated_at" in sys_cols, sys_cols
        con.close()

        # Шаг 4: повторный вызов init_engine идемпотентен.
        db._engine = None  # type: ignore[attr-defined]
        db._SessionLocal = None  # type: ignore[attr-defined]
        db.init_engine(db_path)

        # Шаг 5: имитируем «запись» 2FA-запроса — это раньше и валилось.
        rid = db.open_2fa_request(kind="sms")
        assert isinstance(rid, int) and rid > 0
        st = db.get_auth_state()
        assert st["status"] == "need_2fa"
        assert st["pending_2fa_kind"] == "sms"
        assert st["pending_2fa_request_id"] == rid

        db.put_2fa_code(rid, "1234")
        assert db.take_pending_2fa_code(rid) == "1234"
        db.clear_2fa_request()
        st = db.get_auth_state()
        assert st["pending_2fa_kind"] is None

    # ---- Шаг 6: отдельный сценарий миграции ``topic_sync_jobs.chat_type`` ----
    # «Старая» БД: таблица есть, но без колонки ``chat_type``. После
    # ``init_engine`` колонка должна появиться через ``ALTER TABLE``, и
    # enqueue/claim джобов должны работать с типом чата.
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "bridge_chat_type.db")
        con = sqlite3.connect(db_path)
        try:
            con.executescript(LEGACY_TOPIC_SYNC_JOBS_DDL)
            # Сразу подложим один «pending»-джоб, чтобы убедиться, что
            # миграция не сломает существующие строки.
            con.execute(
                "INSERT INTO topic_sync_jobs (owner_user_id, max_chat_id, "
                "chat_title, action, status) VALUES (?, ?, ?, ?, ?)",
                (1, "legacy-id", "Legacy chat", "create", "pending"),
            )
            con.commit()
        finally:
            con.close()

        # До миграции колонки chat_type быть не должно.
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        legacy_tj_cols = _column_names(cur, "topic_sync_jobs")
        assert "chat_type" not in legacy_tj_cols, legacy_tj_cols
        con.close()

        # init_engine должен применить авто-миграцию.
        db._engine = None  # type: ignore[attr-defined]
        db._SessionLocal = None  # type: ignore[attr-defined]
        db.init_engine(db_path)

        # Проверяем, что колонка появилась.
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        new_tj_cols = _column_names(cur, "topic_sync_jobs")
        assert "chat_type" in new_tj_cols, new_tj_cols
        con.close()

        # И что старая строка сохранилась, а новая с chat_type пишется/читается.
        from shared.db._models import (  # noqa: WPS433
            SuperGroup as _SG,
            TopicSyncJob as _TSJ,
        )
        with db.session_scope() as s:
            rows = s.query(_TSJ).all()
            assert rows, "после миграции ожидается старая строка в БД"
            assert any(r.max_chat_id == "legacy-id" for r in rows), rows

        # Постановка нового джоба с chat_type должна работать.
        with db.session_scope() as s:
            # Нужен владелец + supergroup, чтобы enqueue прошёл непустым списком.
            s.add(_SG(owner_user_id=42, supergroup_chat_id=12345))
            s.flush()
        ids = db.enqueue_topic_sync_jobs(
            owner_user_id=42,
            chats=[
                {"max_chat_id": "dialog-1", "title": "Иван", "type": "DIALOG"},
                {"max_chat_id": "group-1", "title": "Команда", "type": "CHAT"},
                {"max_chat_id": "channel-1", "title": "Новости", "type": "CHANNEL"},
            ],
            supergroup_chat_id=12345,
        )
        assert len(ids) == 3, ids

        # claim должен вернуть все три с корректным chat_type.
        claimed = db.claim_pending_topic_jobs(limit=10)
        assert len(claimed) == 3, [j.max_chat_id for j in claimed]
        by_cid = {str(j.max_chat_id): j for j in claimed}
        assert by_cid["dialog-1"].chat_type == "DIALOG", by_cid
        assert by_cid["group-1"].chat_type == "CHAT", by_cid
        assert by_cid["channel-1"].chat_type == "CHANNEL", by_cid

    # ---- Шаг 7: миграция reaction_ops_queue (новая таблица для реакций) ----
    # На свежей БД таблица должна создаться автоматически через
    # ``Base.metadata.create_all``. Проверяем enqueue/claim/finish
    # для всех направлений.
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "bridge_reactions.db")
        # Сбрасываем engine, чтобы init_engine сработал заново.
        db._engine = None  # type: ignore[attr-defined]
        db._SessionLocal = None  # type: ignore[attr-defined]
        db.init_engine(db_path)

        # Подложим владельца + supergroup (нужно для внешних ключей в
        # ``DeliveredMessage``).
        with db.session_scope() as s:
            from shared.db._models import SuperGroup as _SG, DeliveredMessage as _DM  # noqa: WPS433
            s.add(_SG(owner_user_id=1, supergroup_chat_id=12345))
            # И запись доставки, чтобы можно было ставить tg_summary_message_id.
            s.add(_DM(
                max_chat_id="42",
                max_message_id="100",
                tg_chat_id=12345,
                tg_message_id=999,
            ))
            s.flush()

        # Enqueue ``to_max`` add.
        item_id = db.enqueue_reaction_op({
            "direction": "to_max",
            "op": "add",
            "max_chat_id": "42",
            "max_message_id": "100",
            "emoji": "👍",
        })
        assert item_id > 0

        # Enqueue ``to_tg`` (без emoji — бот возьмёт из reaction_info).
        item_id_2 = db.enqueue_reaction_op({
            "direction": "to_tg",
            "op": "add",
            "max_chat_id": "42",
            "max_message_id": "100",
            "emoji": "🔥",
        })
        assert item_id_2 > item_id

        # Enqueue ``to_tg_summary`` со счётчиками.
        item_id_3 = db.enqueue_reaction_op({
            "direction": "to_tg_summary",
            "op": "summary_update",
            "max_chat_id": "42",
            "max_message_id": "100",
            "counters_json": '[{"reaction":"👍","count":3},{"reaction":"🔥","count":1}]',
            "total_count": 4,
        })
        assert item_id_3 > item_id_2

        # Claim для каждого направления.
        c1 = db.claim_next_reaction_op("to_max")
        assert c1 is not None and c1.direction == "to_max" and c1.op == "add"
        c2 = db.claim_next_reaction_op("to_tg")
        assert c2 is not None and c2.direction == "to_tg"
        c3 = db.claim_next_reaction_op("to_tg_summary")
        assert c3 is not None and c3.direction == "to_tg_summary"
        assert c3.total_count == 4

        # Finish + проверка tg_summary_message_id.
        db.finish_reaction_op(c3.id, ok=True)
        db.set_summary_message_id(
            max_chat_id="42", max_message_id="100", summary_message_id=1001,
        )
        mapping = db.get_delivered_by_max_message(
            max_chat_id="42", max_message_id="100",
        )
        assert mapping is not None
        assert mapping.tg_summary_message_id == 1001
        assert mapping.tg_message_id == 999

        # Невалидный op/direction.
        try:
            db.enqueue_reaction_op({"direction": "bogus", "op": "add"})
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError on invalid direction")

        try:
            db.enqueue_reaction_op({"direction": "to_max", "op": "bogus"})
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError on invalid op")

    print("OK: schema migration applied, SELECT/INSERT/UPDATE работают")
    print("OK: topic_sync_jobs.chat_type мигрирует и прокидывается")
    print("OK: reaction_ops_queue создаётся, enqueue/claim/finish работают")
    return 0


if __name__ == "__main__":
    sys.exit(main())