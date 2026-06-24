"""Слой доступа к общей SQLite-базе моста."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker

Base = declarative_base()


class Event(Base):
    """Входящие сообщения из MAX, ожидающие доставки в Telegram."""

    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    max_chat_id = Column(String, index=True, nullable=False)
    max_message_id = Column(String, index=True, nullable=False)
    chat_title = Column(String, nullable=True)
    sender = Column(String, nullable=True)
    sender_id = Column(String, nullable=True)
    text = Column(Text, nullable=True)
    kind = Column(String, default="text", nullable=False)
    media_path = Column(String, nullable=True)
    media_mime = Column(String, nullable=True)
    media_filename = Column(String, nullable=True)
    media_size = Column(Integer, nullable=True)
    ts = Column(DateTime, default=datetime.utcnow, index=True)
    is_outgoing = Column(Boolean, default=False)
    delivered = Column(Boolean, default=False, index=True)
    delivered_at = Column(DateTime, nullable=True)
    raw_json = Column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint("max_chat_id", "max_message_id", name="uq_chat_msg"),
    )


class Chat(Base):
    """Кэш чатов MAX."""

    __tablename__ = "chats"
    id = Column(Integer, primary_key=True, autoincrement=True)
    max_chat_id = Column(String, unique=True, index=True, nullable=False)
    title = Column(String, nullable=True)
    type = Column(String, nullable=True)
    last_preview = Column(Text, nullable=True)
    last_ts = Column(DateTime, nullable=True)
    unread = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


class SendQueue(Base):
    """Очередь команд на отправку в MAX."""

    __tablename__ = "send_queue"
    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String, default="text", nullable=False)
    target_chat_id = Column(String, index=True, nullable=False)
    text = Column(Text, nullable=True)
    media_path = Column(String, nullable=True)
    media_mime = Column(String, nullable=True)
    media_filename = Column(String, nullable=True)
    created_by = Column(Integer, nullable=True)
    status = Column(String, default="pending", index=True)  # pending, sent, failed
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    # Если пользователь ответил из топика в Telegram — бот сохраняет
    # ``thread_id`` здесь, чтобы MAX-процесс мог пометить этот топик как
    # прочитанный (``mark_chat_read_up_to`` уже используется в боте; ниже
    # мы просто дополним thread_id в sender.py).
    thread_id = Column(Integer, nullable=True)


class AuthState(Base):
    """Текущее состояние авторизации MAX.

    Возможные ``status``:
      * ``ok``              — сессия валидна, мост работает.
      * ``need_2fa``        — MAX прислал запрос на SMS/пароль, ждём /code.
      * ``rate_limited``    — MAX ограничил запросы (``error.limit.violate``).
      * ``auth_required``   — нет валидной сессии; supervisor НИЧЕГО не делает,
                              пока владелец не пришлёт команду через бота
                              (см. ``pending_action``).
      * ``session_attached``— владелец вручную положил session-файл в кэш,
                              supervisor ещё не пробовал подключаться.
      * ``unknown``         — стартовое состояние.

    ``pending_action`` — команда от владельца, которую supervisor должен
    выполнить на следующей итерации:
      * ``"sms"``     — поднять Client, начать SMS-авторизацию.
      * ``"session"`` — поднять Client, попробовать по сохранённой сессии.
      * ``"cancel"``  — отменить текущее действие.
      * ``NULL``      — действий нет, supervisor ждёт.
    """

    __tablename__ = "auth_state"
    id = Column(Integer, primary_key=True, autoincrement=True)
    status = Column(String, default="unknown", index=True)
    pending_2fa_request_id = Column(Integer, nullable=True)
    pending_2fa_kind = Column(String, nullable=True)  # "sms" | "password" | None
    last_2fa_request_at = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    # Команда от владельца для supervisor'а (см. docstring выше).
    pending_action = Column(String, nullable=True)
    # Путь к загруженному session-файлу (если владелец делал upload).
    session_file_path = Column(String, nullable=True)
    # Сообщение для бота, которое надо показать владельцу при следующем тике
    # AuthWatcher (например, "session uploaded, size=12345"). После показа
    # supervisor/bot сами очищают, чтобы не дёргать повторно.
    notify_message = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


class SystemState(Base):
    """Произвольный k/v-сторейдж для служебных данных (используется редко)."""

    __tablename__ = "system_state"
    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


class DeliveredMessage(Base):
    """Сообщения из MAX, доставленные ботом в Telegram.

    Когда ``EventPoller`` отправляет сообщение в TG (``forward_event``),
    мы записываем сюда ``(max_chat_id, max_message_id, delivered_at)``.
    Это «базовая линия» для пометки прочитанным: MAX-процесс помечает
    прочитанными только те сообщения, которые доставлены в TG и пользователь
    как-то отреагировал (см. ``ChatReadState``).
    """

    __tablename__ = "delivered_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    max_chat_id = Column(String, index=True, nullable=False)
    max_message_id = Column(String, index=True, nullable=False)
    delivered_at = Column(DateTime, default=datetime.utcnow, index=True)
    read_at = Column(DateTime, nullable=True, index=True)


class ChatReadState(Base):
    """Время последнего «прочтения» чата пользователем в TG-боте.

    Любое действие пользователя (``REPLY``, ``SHOWID``, ``HISTORY``, ввод
    текста через ``/reply`` и т.п.) обновляет ``last_read_at = now()``.
    MAX-процесс периодически берёт все сообщения чата с
    ``delivered_at <= last_read_at`` и ``read_at IS NULL`` и помечает
    их прочитанными через ``client.read_message``.
    """

    __tablename__ = "chat_read_state"
    max_chat_id = Column(String, primary_key=True)
    last_read_at = Column(DateTime, default=datetime.utcnow)


class SuperGroup(Base):
    """Telegram private supergroup (1 per owner), в которую бот пересылает
    события из MAX. Создаётся через ``/setup``, используется всеми
    forward-ами бота для этого пользователя.

    Хранит ``invite_link``, чтобы пользователь мог повторно открыть
    приватную группу (или поделиться с доверенным лицом).
    """

    __tablename__ = "super_groups"
    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_user_id = Column(Integer, unique=True, index=True, nullable=False)
    supergroup_chat_id = Column(Integer, nullable=False)
    title = Column(String, nullable=True)
    invite_link = Column(String, nullable=True)
    # Запоминаем, включён ли forum mode — чтобы при первом запуске можно
    # было понять, нужно ли включать ``is_forum`` (Bot API 7.0+).
    is_forum_enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ChatTopic(Base):
    """Связь между MAX-чатом и Telegram-топиком в супергруппе.

    Каждый уникальный ``max_chat_id`` получает свой топик внутри
    supergroup. Топики создаются автоматически при первом входящем
    сообщении из MAX (``forwarder.py::get_or_create_topic``).
    """

    __tablename__ = "chat_topics"
    id = Column(Integer, primary_key=True, autoincrement=True)
    max_chat_id = Column(String, unique=True, index=True, nullable=False)
    supergroup_chat_id = Column(Integer, nullable=False)
    thread_id = Column(Integer, nullable=False)
    # Сохраняем чистое имя (chat.title из MAX) — без префикса "(MAX: ...)",
    # который мы добавляем при создании топика.
    topic_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


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


logger = logging.getLogger(__name__)


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
        from sqlalchemy import text

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
                # Если колонка NOT NULL без Python-default, ALTER провалится;
                # в наших моделях таких не должно быть, но на всякий случай
                # приведём к NULL, чтобы не сломать уже лежащие данные.
                if not column.nullable and (
                    column.default is None and column.server_default is None
                ):
                    nullable = "NULL"
                ddl = (
                    f"ALTER TABLE {table_name} "
                    f"ADD COLUMN {column.name} {type_name} {nullable}"
                )
                logger.info("schema migration: %s", ddl)
                conn.execute(text(ddl))


def init_engine(db_path: str) -> None:
    """Инициализирует глобальный engine и создаёт таблицы."""

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
    # Гарантируем наличие одной записи AuthState
    with session_scope() as s:
        if not s.query(AuthState).first():
            s.add(AuthState(status="unknown"))


def get_engine():
    if _engine is None:
        raise RuntimeError("DB engine не инициализирован: вызовите init_engine()")
    return _engine


@contextmanager
def session_scope() -> Iterator:
    """Контекстный менеджер сессии с коммитом/откатом."""

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


# --- Вспомогательные функции для событий/чатов/очереди/авторизации ---


def upsert_event(event: dict) -> Optional[int]:
    """Вставляет новое событие; возвращает id, либо None если дубль."""

    with session_scope() as s:
        existing = s.execute(
            select(Event).where(
                Event.max_chat_id == event["max_chat_id"],
                Event.max_message_id == event["max_message_id"],
            )
        ).scalar_one_or_none()
        if existing:
            return None
        e = Event(
            max_chat_id=event["max_chat_id"],
            max_message_id=event["max_message_id"],
            chat_title=event.get("chat_title"),
            sender=event.get("sender"),
            sender_id=event.get("sender_id"),
            text=event.get("text"),
            kind=event.get("kind", "text"),
            media_path=event.get("media_path"),
            media_mime=event.get("media_mime"),
            media_filename=event.get("media_filename"),
            media_size=event.get("media_size"),
            ts=event.get("timestamp") or datetime.utcnow(),
            is_outgoing=event.get("is_outgoing", False),
            delivered=False,
            raw_json=event.get("raw_json"),
        )
        s.add(e)
        s.flush()
        return e.id


def mark_event_delivered(event_id: int) -> None:
    with session_scope() as s:
        e = s.get(Event, event_id)
        if not e:
            return
        e.delivered = True
        e.delivered_at = datetime.utcnow()


def list_undelivered_events(limit: int = 50) -> List[Event]:
    with session_scope() as s:
        rows = (
            s.execute(
                select(Event)
                .where(Event.delivered.is_(False))
                .order_by(Event.ts.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        s.expunge_all()
        return list(rows)


def list_events_for_chat(max_chat_id: str, limit: int = 20) -> List[Event]:
    with session_scope() as s:
        rows = (
            s.execute(
                select(Event)
                .where(Event.max_chat_id == max_chat_id)
                .order_by(Event.ts.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        s.expunge_all()
        return list(rows)


def upsert_chat(chat: dict) -> None:
    with session_scope() as s:
        existing = s.execute(
            select(Chat).where(Chat.max_chat_id == chat["max_chat_id"])
        ).scalar_one_or_none()
        if existing:
            existing.title = chat.get("title", existing.title)
            existing.type = chat.get("type", existing.type)
            existing.last_preview = chat.get("last_message_preview", existing.last_preview)
            existing.last_ts = chat.get("last_message_at", existing.last_ts)
            existing.unread = chat.get("unread", existing.unread)
            existing.updated_at = datetime.utcnow()
        else:
            s.add(
                Chat(
                    max_chat_id=chat["max_chat_id"],
                    title=chat.get("title"),
                    type=chat.get("type"),
                    last_preview=chat.get("last_message_preview"),
                    last_ts=chat.get("last_message_at"),
                    unread=chat.get("unread"),
                )
            )


def list_chats(limit: int = 100) -> List[Chat]:
    with session_scope() as s:
        rows = (
            s.execute(select(Chat).order_by(Chat.last_ts.desc().nullslast()).limit(limit))
            .scalars()
            .all()
        )
        s.expunge_all()
        return list(rows)


def enqueue_send(item: dict) -> int:
    with session_scope() as s:
        row = SendQueue(
            kind=item.get("kind", "text"),
            target_chat_id=item["target_chat_id"],
            text=item.get("text"),
            media_path=item.get("media_path"),
            media_mime=item.get("media_mime"),
            media_filename=item.get("media_filename"),
            created_by=item.get("created_by"),
            status="pending",
        )
        thread_id = item.get("thread_id")
        if thread_id is not None:
            row.thread_id = int(thread_id)
        s.add(row)
        s.flush()
        return row.id


def claim_next_send() -> Optional[SendQueue]:
    """Атомарно берёт следующую задачу из очереди и помечает ``in_progress``."""

    from sqlalchemy import update

    with session_scope() as s:
        row = s.execute(
            select(SendQueue)
            .where(SendQueue.status == "pending")
            .order_by(SendQueue.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        if not row:
            return None
        row.status = "in_progress"
        s.flush()
        s.expunge(row)
        return row


def finish_send(item_id: int, ok: bool, error: Optional[str] = None) -> None:
    from sqlalchemy import update

    with session_scope() as s:
        s.execute(
            update(SendQueue)
            .where(SendQueue.id == item_id)
            .values(
                status="sent" if ok else "failed",
                error=error,
                finished_at=datetime.utcnow(),
            )
        )


def queue_stats() -> dict:
    with session_scope() as s:
        pending = s.query(SendQueue).filter(SendQueue.status == "pending").count()
        in_progress = s.query(SendQueue).filter(SendQueue.status == "in_progress").count()
        failed = s.query(SendQueue).filter(SendQueue.status == "failed").count()
        sent = s.query(SendQueue).filter(SendQueue.status == "sent").count()
        return {"pending": pending, "in_progress": in_progress, "failed": failed, "sent": sent}


def get_auth_state() -> dict:
    with session_scope() as s:
        row = s.query(AuthState).first()
        if not row:
            row = AuthState(status="unknown")
            s.add(row)
            s.flush()
        return {
            "status": row.status,
            "pending_2fa_request_id": row.pending_2fa_request_id,
            "pending_2fa_kind": row.pending_2fa_kind,
            "last_2fa_request_at": row.last_2fa_request_at,
            "last_login_at": row.last_login_at,
            "last_error": row.last_error,
            # Новые поля (модель «только по команде»).
            "pending_action": row.pending_action,
            "session_file_path": row.session_file_path,
            "notify_message": row.notify_message,
            "updated_at": row.updated_at,
        }


def set_pending_action(action: Optional[str]) -> None:
    """Поставить (или сбросить) команду от владельца для supervisor'а.

    ``action`` — ``"sms"`` / ``"session"`` / ``"cancel"`` / ``None``.
    Используется ботом (и API) для передачи команды supervisor'у.
    """
    with session_scope() as s:
        row = s.query(AuthState).first()
        if not row:
            row = AuthState(status="unknown")
            s.add(row)
        row.pending_action = action
        row.updated_at = datetime.utcnow()


def consume_pending_action() -> Optional[str]:
    """Забрать текущую команду от владельца (и сбросить в NULL).

    Вызывается supervisor'ом после того, как он начал её обрабатывать.
    Возвращает ``"sms"`` / ``"session"`` / ``"cancel"`` / ``None``.
    """
    with session_scope() as s:
        row = s.query(AuthState).first()
        if not row:
            return None
        action = row.pending_action
        row.pending_action = None
        return action


def set_notify_message(text: Optional[str]) -> None:
    """Положить одноразовое сообщение для AuthWatcher (бота).

    AuthWatcher заберёт его через ``consume_notify_message`` и сразу очистит,
    чтобы не показывать повторно.
    """
    with session_scope() as s:
        row = s.query(AuthState).first()
        if not row:
            row = AuthState(status="unknown")
            s.add(row)
        row.notify_message = text
        row.updated_at = datetime.utcnow()


def consume_notify_message() -> Optional[str]:
    """Забрать (и сбросить) одноразовое сообщение для AuthWatcher."""
    with session_scope() as s:
        row = s.query(AuthState).first()
        if not row:
            return None
        msg = row.notify_message
        row.notify_message = None
        return msg


def set_session_file_path(path: Optional[str]) -> None:
    """Запомнить путь к загруженному session-файлу (или сбросить)."""
    with session_scope() as s:
        row = s.query(AuthState).first()
        if not row:
            row = AuthState(status="unknown")
            s.add(row)
        row.session_file_path = path
        row.updated_at = datetime.utcnow()


def set_auth_state(
    status: str,
    error: Optional[str] = None,
    last_login: bool = False,
    clear_error: bool = False,
) -> None:
    """Обновить auth_state.

    Параметры:
      * ``status`` — новый статус (ok/need_2fa/rate_limited/unknown/...)
      * ``error`` — если не ``None``, записывается в ``last_error``
      * ``clear_error`` — если True, ``last_error`` сбрасывается в ``NULL``
        даже если ``error is None``. Нужно для случаев, когда max-процесс
        хочет «очистить» предыдущую ошибку (например, после успешного
        start() или после ручного reauth). Без этого supervisor.post_auth_state
        всегда передаёт ``error: null`` в API, и поле никогда не очищается.
      * ``last_login`` — обновить ``last_login_at`` (используется при status=ok)
    """
    with session_scope() as s:
        row = s.query(AuthState).first()
        if not row:
            row = AuthState(status=status)
            s.add(row)
        row.status = status
        if error is not None:
            row.last_error = error
        elif clear_error:
            row.last_error = None
        if last_login:
            row.last_login_at = datetime.utcnow()
        row.updated_at = datetime.utcnow()


def open_2fa_request(kind: str = "sms") -> int:
    """Создаёт pending-запрос 2FA. ``kind`` — ``"sms"`` или ``"password"``."""
    if kind not in ("sms", "password"):
        kind = "sms"
    with session_scope() as s:
        row = s.query(AuthState).first()
        if not row:
            row = AuthState(status="need_2fa")
            s.add(row)
        row.status = "need_2fa"
        row.pending_2fa_request_id = int(datetime.utcnow().timestamp() * 1000)
        row.pending_2fa_kind = kind
        row.last_2fa_request_at = datetime.utcnow()
        row.updated_at = datetime.utcnow()
        return row.pending_2fa_request_id


def take_pending_2fa_code(request_id: int) -> Optional[str]:
    """Сохраняем 2FA-код в SystemState, чтобы watcher мог его забрать."""

    key = f"2fa_code:{request_id}"
    with session_scope() as s:
        row = s.query(SystemState).filter(SystemState.key == key).first()
        if not row:
            return None
        code = row.value
        s.delete(row)
        return code


def put_2fa_code(request_id: int, code: str) -> None:
    key = f"2fa_code:{request_id}"
    with session_scope() as s:
        row = s.query(SystemState).filter(SystemState.key == key).first()
        if row:
            row.value = code
            row.updated_at = datetime.utcnow()
        else:
            s.add(SystemState(key=key, value=code))


def list_2fa_code_keys() -> List[int]:
    """Список request_id (int), для которых владелец уже положил код/пароль
    в ``system_state`` (ключ вида ``2fa_code:<rid>``).

    Используется max-процессом (supervisor) для фонового «drain» —
    чтобы разбудить локальный ``asyncio.Event`` в ``app.auth`` после того,
    как бот положил код через ``/code``.
    """
    with session_scope() as s:
        rows = (
            s.query(SystemState)
            .filter(SystemState.key.like("2fa_code:%"))
            .all()
        )
        out: List[int] = []
        for r in rows:
            try:
                out.append(int(r.key.split(":", 1)[1]))
            except (ValueError, IndexError):
                continue
        return out


def clear_2fa_request() -> None:
    with session_scope() as s:
        row = s.query(AuthState).first()
        if not row:
            return
        row.pending_2fa_request_id = None
        row.pending_2fa_kind = None
        row.updated_at = datetime.utcnow()


# --- Доставка в TG + пометка прочитанным ---


def record_delivered(max_chat_id: str, max_message_id: str) -> None:
    """Записать факт доставки сообщения из MAX в TG-бот.

    Используется из API при вызове ``POST /events/{id}/delivered``.
    Идемпотентно: если запись уже есть, обновляет ``delivered_at``.
    """
    from sqlalchemy import update

    with session_scope() as s:
        existing = (
            s.query(DeliveredMessage)
            .filter(
                DeliveredMessage.max_chat_id == str(max_chat_id),
                DeliveredMessage.max_message_id == str(max_message_id),
            )
            .first()
        )
        if existing:
            existing.delivered_at = datetime.utcnow()
            s.flush()
        else:
            s.add(DeliveredMessage(
                max_chat_id=str(max_chat_id),
                max_message_id=str(max_message_id),
                delivered_at=datetime.utcnow(),
            ))


def update_chat_read_state(max_chat_id: str, read_at: Optional[datetime] = None) -> None:
    """Обновить ``last_read_at`` для чата (вызывается при любом действии пользователя).

    Идемпотентно: только увеличивает ``last_read_at`` (никогда не уменьшает).
    """
    when = read_at or datetime.utcnow()
    with session_scope() as s:
        existing = (
            s.query(ChatReadState)
            .filter(ChatReadState.max_chat_id == str(max_chat_id))
            .first()
        )
        if existing:
            if when > existing.last_read_at:
                existing.last_read_at = when
        else:
            s.add(ChatReadState(
                max_chat_id=str(max_chat_id),
                last_read_at=when,
            ))


def get_pending_read_receipts(limit: int = 100) -> List[DeliveredMessage]:
    """MAX-процесс забирает все доставленные, но ещё не помеченные
    прочитанными сообщения, у которых ``delivered_at <= min(chat.last_read_at)``.
    Возвращает ``DeliveredMessage`` с непустым ``read_at = None``.
    """
    with session_scope() as s:
        rows = (
            s.execute(
                select(DeliveredMessage)
                .join(
                    ChatReadState,
                    DeliveredMessage.max_chat_id == ChatReadState.max_chat_id,
                    isouter=False,
                )
                .where(
                    DeliveredMessage.read_at.is_(None),
                    DeliveredMessage.delivered_at <= ChatReadState.last_read_at,
                )
                .order_by(DeliveredMessage.delivered_at.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        s.expunge_all()
        return list(rows)


def mark_delivered_as_read(delivered_id: int) -> None:
    """MAX-процесс вызывает после успешного ``client.read_message``.
    Проставляет ``read_at = now()`` — повторно брать эту запись не будем.
    """
    with session_scope() as s:
        row = s.get(DeliveredMessage, delivered_id)
        if not row:
            return
        row.read_at = datetime.utcnow()


# --- Telegram supergroup + topics ---


def get_supergroup_for_owner(owner_user_id: int) -> Optional[SuperGroup]:
    """Возвращает supergroup владельца или ``None`` если ещё не создал."""
    with session_scope() as s:
        row = (
            s.query(SuperGroup)
            .filter(SuperGroup.owner_user_id == int(owner_user_id))
            .first()
        )
        if not row:
            return None
        s.expunge(row)
        return row


def create_supergroup(
    owner_user_id: int,
    supergroup_chat_id: int,
    title: str,
    invite_link: Optional[str] = None,
    is_forum_enabled: bool = True,
) -> None:
    """Создать запись о приватной supergroup для пользователя.

    Если запись уже существует — обновляет ``supergroup_chat_id`` / ``invite_link``.
    """
    with session_scope() as s:
        existing = (
            s.query(SuperGroup)
            .filter(SuperGroup.owner_user_id == int(owner_user_id))
            .first()
        )
        if existing:
            existing.supergroup_chat_id = int(supergroup_chat_id)
            existing.title = title
            existing.invite_link = invite_link
            existing.is_forum_enabled = bool(is_forum_enabled)
            return
        s.add(SuperGroup(
            owner_user_id=int(owner_user_id),
            supergroup_chat_id=int(supergroup_chat_id),
            title=title,
            invite_link=invite_link,
            is_forum_enabled=bool(is_forum_enabled),
        ))


def update_supergroup_invite_link(
    owner_user_id: int, invite_link: str
) -> None:
    """Обновить ``invite_link`` (например, после ``export_chat_invite_link``)."""
    with session_scope() as s:
        row = (
            s.query(SuperGroup)
            .filter(SuperGroup.owner_user_id == int(owner_user_id))
            .first()
        )
        if row:
            row.invite_link = invite_link


def get_topic(max_chat_id: str) -> Optional[ChatTopic]:
    """Возвращает топик для ``max_chat_id`` или ``None``."""
    with session_scope() as s:
        row = (
            s.query(ChatTopic)
            .filter(ChatTopic.max_chat_id == str(max_chat_id))
            .first()
        )
        if not row:
            return None
        s.expunge(row)
        return row


def get_topic_by_thread_id(
    supergroup_chat_id: int, thread_id: int
) -> Optional[ChatTopic]:
    """Обратный lookup: топик по ``(supergroup_chat_id, thread_id)``.

    Используется «эхо»-хэндлером бота, который при получении сообщения
    в топике супергруппы должен понять, в какой MAX-чат его отправить.
    Возвращает ``None``, если такого топика нет (например, кто-то
    вручную создал топик в группе или привязка ещё не появилась).
    """
    with session_scope() as s:
        row = (
            s.query(ChatTopic)
            .filter(
                ChatTopic.supergroup_chat_id == int(supergroup_chat_id),
                ChatTopic.thread_id == int(thread_id),
            )
            .first()
        )
        if not row:
            return None
        s.expunge(row)
        return row


def create_topic(
    max_chat_id: str,
    supergroup_chat_id: int,
    thread_id: int,
    topic_name: Optional[str] = None,
) -> None:
    """Создать запись о топике. Идемпотентно: если уже есть — не трогает."""
    with session_scope() as s:
        existing = (
            s.query(ChatTopic)
            .filter(ChatTopic.max_chat_id == str(max_chat_id))
            .first()
        )
        if existing:
            return
        s.add(ChatTopic(
            max_chat_id=str(max_chat_id),
            supergroup_chat_id=int(supergroup_chat_id),
            thread_id=int(thread_id),
            topic_name=topic_name,
        ))


def update_topic_name(max_chat_id: str, topic_name: str) -> None:
    """Обновить ``topic_name`` (например, при ``on_chat_update`` из MAX)."""
    with session_scope() as s:
        row = (
            s.query(ChatTopic)
            .filter(ChatTopic.max_chat_id == str(max_chat_id))
            .first()
        )
        if row and row.topic_name != topic_name:
            row.topic_name = topic_name


def list_topics() -> List[ChatTopic]:
    """Все топики (используется при старте для логов)."""
    with session_scope() as s:
        rows = (
            s.query(ChatTopic)
            .order_by(ChatTopic.created_at.asc())
            .all()
        )
        s.expunge_all()
        return list(rows)
