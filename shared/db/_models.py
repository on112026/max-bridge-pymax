"""ORM-модели для общей SQLite-БД моста MAX ↔ Telegram.

Все таблицы описаны здесь в одном месте, чтобы было легко увидеть общую
картину. Функции для работы с этими таблицами разнесены по доменам в
``shared/db/``:

* ``events`` — входящие/прочитанные события из MAX.
* ``chats`` — кэш MAX-чатов.
* ``send_queue`` — очередь отправки в MAX.
* ``auth_state`` — состояние авторизации MAX.
* ``read_receipts`` — пометки «прочитано в TG» → MAX.
* ``supergroups`` — привязанные Telegram supergroups.
* ``topics`` — связки MAX-чат ↔ Telegram-топик.
* ``topic_jobs`` — очередь задач синка топиков (создание/переименование).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base

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

    ``stale`` — флаг «MAX-чат пропал»:
      * ``0`` — живой чат, топик актуален (по умолчанию).
      * ``1`` — MAX-чат не найден в свежем sync (``fetch_chats``),
        но топик в Telegram ещё открыт. Показываем в ``/status`` и
        предлагаем ``/prune_topics``.
      * ``2`` — владелец явно закрыл топик (``closeForumTopic``)
        или пометил его как устаревший; больше не показываем.
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
    # Признак «MAX-чат пропал». 0 — живой, 1 — stale, 2 — закрыт.
    stale = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow)


class TopicSyncJob(Base):
    """Очередь задач на создание/переименование топика в Telegram.

    Max-процесс (в ``_on_start``) при auth=ok заливает сюда пачку
    задач через ``POST /internal/sync_topics``. Bot-процесс (TopicSyncWorker)
    раз в 2 секунды забирает pending-джобы и через ``createForumTopic`` /
    ``editForumTopic`` создаёт/переименовывает топики, помечая джоб done/failed.
    """

    __tablename__ = "topic_sync_jobs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_user_id = Column(Integer, index=True, nullable=False)
    max_chat_id = Column(String, index=True, nullable=False)
    chat_title = Column(String, nullable=True)
    # "create" | "rename" — что именно должен сделать бот.
    action = Column(String, nullable=False)
    # "pending" | "in_progress" | "done" | "failed".
    status = Column(String, nullable=False, default="pending")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    # Количество попыток (для backoff в worker'е).
    attempts = Column(Integer, nullable=False, default=0)