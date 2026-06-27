"""Pydantic-схемы для HTTP API моста MAX ↔ Telegram.

Все request/response модели собраны здесь в одном месте, чтобы было
легко увидеть общую картину. Роутеры (``api/routers/``) импортируют
только нужные им схемы.

Принципы:

* Документирующие docstring у каждого класса — описание для OpenAPI.
* ``Optional[...]`` для всех полей, которые могут отсутствовать во
  входящих payload'ах от pymax.
* Для дат-строк — ``str`` (ISO-формат), парсинг в ``datetime`` делаем
  в роутере.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


# ---------- Events ----------


class EventIn(BaseModel):
    max_chat_id: str
    max_message_id: str
    chat_title: Optional[str] = None
    sender: Optional[str] = None
    sender_id: Optional[str] = None
    text: Optional[str] = None
    kind: str = "text"
    media_path: Optional[str] = None
    media_mime: Optional[str] = None
    media_filename: Optional[str] = None
    media_size: Optional[int] = None
    timestamp: Optional[str] = None
    is_outgoing: bool = False


class EventOut(BaseModel):
    id: int
    max_chat_id: str
    max_message_id: str
    chat_title: Optional[str] = None
    sender: Optional[str] = None
    sender_id: Optional[str] = None
    text: Optional[str] = None
    kind: str
    media_path: Optional[str] = None
    media_mime: Optional[str] = None
    media_filename: Optional[str] = None
    media_size: Optional[int] = None
    timestamp: Optional[str] = None
    is_outgoing: bool


# ---------- Chats ----------


class ChatIn(BaseModel):
    max_chat_id: str
    title: Optional[str] = None
    type: Optional[str] = None
    last_message_preview: Optional[str] = None
    last_message_at: Optional[str] = None
    unread: Optional[int] = None


class ChatOut(BaseModel):
    max_chat_id: str
    title: Optional[str] = None
    type: Optional[str] = None
    last_message_preview: Optional[str] = None
    last_message_at: Optional[str] = None
    unread: Optional[int] = None


# ---------- Send ----------


class SendIn(BaseModel):
    kind: str = "text"
    target_chat_id: str
    text: Optional[str] = None
    media_path: Optional[str] = None
    media_mime: Optional[str] = None
    media_filename: Optional[str] = None
    created_by: Optional[int] = None
    # ``thread_id`` — id топика в Telegram supergroup, из которого
    # пользователь отправил сообщение. Передаётся через ``enqueue_send``
    # в ``SendQueue.thread_id`` (см. ``shared/db.py::SendQueue``).
    thread_id: Optional[int] = None


class SendOut(BaseModel):
    id: int
    kind: str
    target_chat_id: str
    text: Optional[str] = None
    media_path: Optional[str] = None
    media_mime: Optional[str] = None
    media_filename: Optional[str] = None
    status: str
    error: Optional[str] = None
    created_at: Optional[str] = None
    finished_at: Optional[str] = None
    thread_id: Optional[int] = None


# ---------- Status ----------


class StatusOut(BaseModel):
    auth: dict
    queue: dict
    undelivered: int
    chats: int


class OkOut(BaseModel):
    ok: bool = True


# ---------- Auth ----------


class AuthStateIn(BaseModel):
    status: str
    error: Optional[str] = None
    # Если True — ``last_error`` сбрасывается в NULL даже при ``error=None``.
    # Нужно max-процессу, чтобы «очистить» предыдущую ошибку при status=ok
    # или при ручном reauth (если раньше была rate-limit-ошибка).
    clear_error: bool = False


class TwoFaRequestIn(BaseModel):
    kind: str = "sms"  # "sms" | "password"


class TwoFaRequestOut(BaseModel):
    request_id: int
    kind: str


class TwoFaCodeIn(BaseModel):
    request_id: int
    code: str


class TwoFaCodeOut(BaseModel):
    code: Optional[str] = None


class AuthActionIn(BaseModel):
    """Команда от бота (владельца) supervisor'у.

    ``action``:
      * ``"sms"``     — поднять Client, начать SMS-авторизацию.
      * ``"session"`` — поднять Client, попробовать по сохранённой сессии.
      * ``"cancel"``  — отменить текущее действие и вернуться в ``auth_required``.
    """

    action: str


class AuthActionOut(BaseModel):
    ok: bool = True
    pending_action: Optional[str] = None


class NotifyOut(BaseModel):
    ok: bool = True
    message: Optional[str] = None


# ---------- Sessions ----------


class SessionUploadOut(BaseModel):
    ok: bool = True
    path: str
    size: int


class SessionInfo(BaseModel):
    name: str
    path: str
    size: int
    modified: float


class SessionListOut(BaseModel):
    sessions: List[SessionInfo]
    current: Optional[str] = None


class SessionUseIn(BaseModel):
    session_name: str


# ---------- Topic jobs ----------


class TopicJobOut(BaseModel):
    id: int
    owner_user_id: int
    max_chat_id: str
    chat_title: Optional[str] = None
    # Тип чата из MAX (``DIALOG`` / ``CHAT`` / ``CHANNEL``) — нужен
    # бот-воркеру, чтобы в имени топика подставлять «ЛС»/«группа»/«канал»
    # вместо безликого «MAX». ``None`` для старых джобов.
    chat_type: Optional[str] = None
    action: str
    attempts: int


class TopicJobList(BaseModel):
    jobs: List[TopicJobOut]


class TopicJobFinishIn(BaseModel):
    ok: bool = True
    error: Optional[str] = None


# ---------- Stale topics ----------


class StaleTopicOut(BaseModel):
    max_chat_id: str
    supergroup_chat_id: int
    thread_id: int
    topic_name: Optional[str] = None


class StaleTopicList(BaseModel):
    topics: List[StaleTopicOut]


class CloseTopicIn(BaseModel):
    """Запрос от бота: пометить топик закрытым (``stale=2``) после
    успешного ``closeForumTopic``. Саму операцию closeForumTopic бот
    выполняет сам (он единственный, у кого есть Bot)."""

    owner_user_id: int


# ---------- Read receipts ----------


class PendingReadReceipt(BaseModel):
    id: int
    max_chat_id: str
    max_message_id: str
    delivered_at: str


class ReadReceiptOk(BaseModel):
    ok: bool = True
    marked: int = 0


# ---------- Sync (internal) ----------


class SyncTopicChat(BaseModel):
    """Один чат из MAX в payload ``/internal/sync_topics``."""

    max_chat_id: str
    title: Optional[str] = None
    type: Optional[str] = None


class SyncTopicsIn(BaseModel):
    """Запрос от max-процесса после ``fetch_chats``.

    ``trigger`` — текстовая метка (``"auth_ok"`` / ``"manual"`` / …),
    сейчас только логируется. ``chats`` — полный список чатов из MAX
    на момент sync'а.
    """

    trigger: Optional[str] = None
    chats: List[SyncTopicChat]


class SyncTopicsOut(BaseModel):
    ok: bool = True
    trigger: Optional[str] = None
    synced_chats: int = 0
    enqueued_jobs: int = 0
    by_action: dict = {}
    stale_topics: int = 0


class NotifyIn(BaseModel):
    """Используется max-процессом, чтобы сообщить api о системных событиях
    (например, «пришёл запрос на SMS»). Сейчас api сам опрашивает auth_state,
    поэтому этот маршрут — no-op, оставлен на будущее (push вместо polling)."""

    event: str
    payload: Optional[dict] = None