"""Инициализация SQLAlchemy engine + миграции схемы + ``session_scope``.

Глобальное состояние:

* ``_engine`` — singleton SQLAlchemy ``Engine`` для SQLite-БД моста.
* ``_SessionLocal`` — ``scoped_session`` поверх ``sessionmaker``.

``init_engine(db_path)`` создаёт engine (если ещё не создан) и
прогоняет ``_apply_schema_migrations`` (ALTER TABLE для колонок,
которые добавились в моделях после первого запуска на проде).

``session_scope()`` — контекстный менеджер с автоматическим
commit/rollback/close. Используется всеми остальными модулями
``shared.db.*``.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker

from shared.db._models import AuthState, Base

logger = logging.getLogger(__name__)


# Маппинг SQLAlchemy-типов на компактные имена SQLite-типов, которые мы
# используем в ``ALTER TABLE ... ADD COLUMN``. Для SQLite формат хранения
# не критичен (``NUMERIC`` / ``TEXT`` / ``INTEGER``), но значения должны
# быть разборчивыми.
_SQLITE_TYPE_MAP = {
    Boolean: "INTEGER",
    DateTime: "DATETIME",
    Integer: "INTEGER",
    String: "VARCHAR",
    Text: "TEXT",
}


_engine = None
_SessionLocal: Optional[scoped_session] = None


def _sqlite_type_name(column) -> str:
    """Возвращает имя типа для ``ALTER TABLE ADD COLUMN``."""
    py_type = column.type
    # Строковые типы SQLAlchemy (String/Text) могут иметь длину.
    if isinstance(py_type, String):
        return "VARCHAR"
    if isinstance(py_type, Text):
        return "TEXT"
    for cls, name in _SQLITE_TYPE_MAP.items():
        if isinstance(py_type, cls):
            return name
    # На крайний случай — TEXT, всё сериализуемо.
    return "TEXT"


def _apply_schema_migrations(engine) -> None:
    """Добавляет недостающие колонки к существующим таблицам.

    ``Base.metadata.create_all`` создаёт только отсутствующие таблицы, но
    не умеет делать ``ALTER TABLE`` для добавления новых колонок. На Railway
    (и вообще на любом томе) старая БД переживает рестарты, и при добавлении
    полей в модели приложение падает с ``sqlite3.OperationalError: no such
    column: ...``. Чтобы этого избежать, проходимся по таблицам из метаданных,
    сравниваем их с ``PRAGMA table_info`` и для отсутствующих колонок делаем
    ``ALTER TABLE ... ADD COLUMN`` (идемпотентно: повторный запуск — no-op).
    """
    insp = engine.dialect.get_columns  # type: ignore[attr-defined]
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            table_name = table.name
            existing = {col["name"] for col in insp(conn, table_name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                # PRIMARY KEY и NOT NULL без DEFAULT в SQLite ALTER TABLE
                # нельзя — у существующих строк нет значения. Все наши PK
                # автоинкрементные и присутствуют с самого начала, поэтому
                # миграция добавляет только nullable-колонки.
                if column.primary_key:
                    logger.warning(
                        "schema migration: cannot add PRIMARY KEY column %s.%s; "
                        "drop the table or run alembic",
                        table_name,
                        column.name,
                    )
                    continue
                type_name = _sqlite_type_name(column)
                nullable = "NULL" if column.nullable else "NOT NULL"
                # Для NOT NULL без SQL DEFAULT SQLite не сможет заполнить
                # существующие строки в ALTER TABLE. Если у колонки есть
                # Python-side default (например, ``Column(Integer,
                # nullable=False, default=0)`` — наш случай с
                # ``chat_topics.stale``), превращаем его в SQL-литерал и
                # дописываем ``DEFAULT <value>`` — тогда миграция пройдёт
                # и существующие строки получат это значение.
                sql_default = ""
                if not column.nullable and column.server_default is None:
                    py_default = (
                        column.default.arg
                        if column.default is not None else None
                    )
                    if py_default is None:
                        # NOT NULL без default — ослабим до NULL, чтобы
                        # миграция не уронила старт контейнера.
                        nullable = "NULL"
                    elif isinstance(py_default, bool):
                        sql_default = f" DEFAULT {1 if py_default else 0}"
                    elif isinstance(py_default, int):
                        sql_default = f" DEFAULT {int(py_default)}"
                    elif isinstance(py_default, float):
                        sql_default = f" DEFAULT {py_default}"
                    elif isinstance(py_default, str):
                        escaped = py_default.replace("'", "''")
                        sql_default = f" DEFAULT '{escaped}'"
                    else:
                        # callable / func.* / неизвестный тип — не пытаемся
                        # угадать, фолбэк на NULL с предупреждением.
                        logger.warning(
                            "schema migration: cannot translate Python "
                            "default %r for %s.%s; falling back to NULL",
                            py_default, table_name, column.name,
                        )
                        nullable = "NULL"
                ddl = (
                    f"ALTER TABLE {table_name} "
                    f"ADD COLUMN {column.name} {type_name} "
                    f"{nullable}{sql_default}"
                )
                logger.info("schema migration: %s", ddl)
                conn.execute(text(ddl))


def init_engine(db_path: str) -> None:
    """Инициализирует глобальный engine и создаёт таблицы.

    Идемпотентна — повторный вызов с тем же путём no-op. ``db_path``
    вида ``/data/bridge.db``; родительский каталог создаётся при
    необходимости.
    """
    global _engine, _SessionLocal
    if _engine is not None:
        return
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    _engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(_engine)
    _apply_schema_migrations(_engine)
    _SessionLocal = scoped_session(
        sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    )
    # Гарантируем наличие одной записи AuthState.
    with session_scope() as s:
        if not s.query(AuthState).first():
            s.add(AuthState(status="unknown"))


def get_engine():
    """Вернуть текущий engine. Бросает ``RuntimeError``, если ``init_engine``
    ещё не вызывали.
    """
    if _engine is None:
        raise RuntimeError("DB engine не инициализирован: вызовите init_engine()")
    return _engine


@contextmanager
def session_scope() -> Iterator:
    """Контекстный менеджер сессии с коммитом/откатом.

    Используется всеми модулями ``shared.db.*``. Бросает ``RuntimeError``,
    если ``init_engine`` ещё не вызывали.
    """
    if _SessionLocal is None:
        raise RuntimeError("DB engine не инициализирован: вызовите init_engine()")
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        _SessionLocal.remove()