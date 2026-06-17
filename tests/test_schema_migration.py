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

    print("OK: schema migration applied, SELECT/INSERT/UPDATE работают")
    return 0


if __name__ == "__main__":
    sys.exit(main())