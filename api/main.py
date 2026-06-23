"""FastAPI-приложение моста MAX ↔ Telegram (этап 2, PyMax).

Без VNC, без headful-прокси, без watcher'а — все 2FA/SMS происходят
через PyMax SmsAuthFlow, а коды владелец вводит в Telegram-боте.

Модель авторизации «только по команде»: на cold-start supervisor ставит
``auth_state.status = auth_required`` и НЕ создаёт PyMax Client. Владелец
через бота нажимает inline-кнопку, и только тогда бот кладёт
``pending_action`` в БД (эндпоинт ``/auth/action``), а supervisor
обрабатывает его на следующей итерации.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

# Подключаем /app/shared, /app/api как путь импорта (контейнерная раскладка)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "api"))

from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from shared import db, models
from shared.api_auth import verify_api_key
from shared.config import load_settings
from shared.log_setup import configure_logging

settings = load_settings()
configure_logging(settings.log_level)
db.init_engine(settings.db_path)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_engine(settings.db_path)
    os.makedirs(settings.media_dir, exist_ok=True)
    yield


app = FastAPI(title="MAX ↔ Telegram Bridge API (PyMax)", version="2.0.0", lifespan=lifespan)


# ---------- Схемы запросов/ответов ----------


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
    # в ``SendQueue.thread_id`` (см. ``shared/db.py``).
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


class StatusOut(BaseModel):
    auth: dict
    queue: dict
    undelivered: int
    chats: int


class OkOut(BaseModel):
    ok: bool = True


def _event_to_out(e) -> EventOut:
    return EventOut(
        id=e.id,
        max_chat_id=e.max_chat_id,
        max_message_id=e.max_message_id,
        chat_title=e.chat_title,
        sender=e.sender,
        sender_id=e.sender_id,
        text=e.text,
        kind=e.kind,
        media_path=e.media_path,
        media_mime=e.media_mime,
        media_filename=e.media_filename,
        media_size=e.media_size,
        timestamp=e.ts.isoformat() if e.ts else None,
        is_outgoing=e.is_outgoing,
    )


def _chat_to_out(c) -> ChatOut:
    return ChatOut(
        max_chat_id=c.max_chat_id,
        title=c.title,
        type=c.type,
        last_message_preview=c.last_preview,
        last_message_at=c.last_ts.isoformat() if c.last_ts else None,
        unread=c.unread,
    )


def _send_to_out(s) -> SendOut:
    return SendOut(
        id=s.id,
        kind=s.kind,
        target_chat_id=s.target_chat_id,
        text=s.text,
        media_path=s.media_path,
        media_mime=s.media_mime,
        media_filename=s.media_filename,
        status=s.status,
        error=s.error,
        created_at=s.created_at.isoformat() if s.created_at else None,
        finished_at=s.finished_at.isoformat() if s.finished_at else None,
        # ``thread_id`` (id TG-топика, из которого отправлено сообщение)
        # — пробрасываем в ответ, чтобы клиент мог проверить, что поле
        # корректно сохранилось в ``SendQueue``. См. ``shared/db.py::SendQueue``.
        thread_id=s.thread_id,
    )


# ---------- Маршруты ----------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def root() -> dict:
    return {"service": "max-bridge-pymax-api", "version": "2.0.0"}


@app.post("/events", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def post_event(event: EventIn) -> OkOut:
    payload = event.model_dump()
    if payload.get("timestamp"):
        from datetime import datetime
        try:
            payload["timestamp"] = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
        except ValueError:
            payload["timestamp"] = None
    db.upsert_event(payload)
    return OkOut(ok=True)


@app.get("/events", response_model=List[EventOut], dependencies=[Depends(verify_api_key)])
def list_events(
        undelivered: bool = Query(default=False),
        limit: int = Query(default=20, ge=1, le=200),
) -> List[EventOut]:
    if undelivered:
        rows = db.list_undelivered_events(limit=limit)
    else:
        with db.session_scope() as s:
            from sqlalchemy import select
            rows = (
                s.execute(select(db.Event).order_by(db.Event.ts.desc()).limit(limit))
                .scalars()
                .all()
            )
            s.expunge_all()
            rows = list(rows)
    return [_event_to_out(r) for r in rows]


@app.get("/events/{event_id}", response_model=EventOut, dependencies=[Depends(verify_api_key)])
def get_event(event_id: int) -> EventOut:
    """Получить одно событие по id (нужно бота-колбэкам reply/showid/history,
    которые получают только короткий ``event_id`` в callback_data из-за
    64-байтного лимита Telegram Bot API на ``callback_data``).
    """
    from fastapi import HTTPException as _HTTPException

    with db.session_scope() as s:
        row = s.get(db.Event, event_id)
        if not row:
            raise _HTTPException(status_code=404, detail="event not found")
        s.expunge(row)
        return _event_to_out(row)


@app.get("/events/by-chat/{chat_id}", response_model=List[EventOut], dependencies=[Depends(verify_api_key)])
def events_by_chat(chat_id: str, limit: int = Query(default=20, ge=1, le=200)) -> List[EventOut]:
    rows = db.list_events_for_chat(chat_id, limit=limit)
    return [_event_to_out(r) for r in rows]


@app.post("/events/{event_id}/delivered", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def mark_event_delivered(event_id: int) -> OkOut:
    db.mark_event_delivered(event_id)
    # Параллельно записываем в ``delivered_messages`` — это источник истины
    # для пометки прочитанным в MAX. Делаем ``best effort``: если событие
    # не найдено в ``events``, пропускаем.
    try:
        with db.session_scope() as s:
            row = s.get(db.Event, event_id)
            if row is not None:
                db.record_delivered(row.max_chat_id, row.max_message_id)
    except Exception as exc:
        logger.warning("record_delivered for event %s failed: %s", event_id, exc)
    return OkOut(ok=True)


# ---------- Read receipts: «прочитано» в TG → MAX ----------


class PendingReadReceipt(BaseModel):
    id: int
    max_chat_id: str
    max_message_id: str
    delivered_at: str


class ReadReceiptOk(BaseModel):
    ok: bool = True
    marked: int = 0


@app.post(
    "/chats/{chat_id}/read-up-to",
    response_model=OkOut,
    dependencies=[Depends(verify_api_key)],
)
def mark_chat_read_up_to(chat_id: str) -> OkOut:
    """Бот вызывает при любом действии пользователя (REPLY, SHOWID, ввод текста).

    Это значит «все сообщения этого чата до этого момента прочитаны».
    MAX-процесс заберёт доставленные сообщения с ``delivered_at <= now``
    через ``GET /chats/pending-reads`` и пометит их в MAX через ``client.read_message``.
    """
    db.update_chat_read_state(chat_id)
    return OkOut(ok=True)


@app.get(
    "/chats/pending-reads",
    response_model=List[PendingReadReceipt],
    dependencies=[Depends(verify_api_key)],
)
def get_pending_reads(limit: int = Query(default=50, ge=1, le=500)) -> List[PendingReadReceipt]:
    """MAX-процесс забирает список доставленных сообщений, которые уже можно
    пометить прочитанными (``delivered_at <= chat.last_read_at``, ``read_at IS NULL``).
    """
    rows = db.get_pending_read_receipts(limit=limit)
    return [
        PendingReadReceipt(
            id=r.id,
            max_chat_id=r.max_chat_id,
            max_message_id=r.max_message_id,
            delivered_at=r.delivered_at.isoformat() if r.delivered_at else "",
        )
        for r in rows
    ]


@app.post(
    "/chats/{chat_id}/messages/{message_id}/read",
    response_model=ReadReceiptOk,
    dependencies=[Depends(verify_api_key)],
)
def mark_message_read(chat_id: str, message_id: str, delivered_id: int = Query(default=0)) -> ReadReceiptOk:
    """MAX-процесс вызывает после успешного ``client.read_message``.
    Проставляет ``read_at = now()`` для записи ``DeliveredMessage`` —
    чтобы больше её не брать.
    """
    if delivered_id > 0:
        db.mark_delivered_as_read(delivered_id)
        return ReadReceiptOk(ok=True, marked=1)
    # Фолбэк: ищем по (chat_id, message_id) и помечаем первую
    # непрочитанную запись.
    with db.session_scope() as s:
        from shared.db import DeliveredMessage
        row = (
            s.query(DeliveredMessage)
            .filter(
                DeliveredMessage.max_chat_id == str(chat_id),
                DeliveredMessage.max_message_id == str(message_id),
                DeliveredMessage.read_at.is_(None),
            )
            .order_by(DeliveredMessage.id.asc())
            .first()
        )
        if row is not None:
            row.read_at = datetime.utcnow()
            return ReadReceiptOk(ok=True, marked=1)
    return ReadReceiptOk(ok=True, marked=0)


@app.post("/chats", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def post_chat(chat: ChatIn) -> OkOut:
    payload = chat.model_dump()
    if payload.get("last_message_at"):
        from datetime import datetime
        try:
            payload["last_message_at"] = datetime.fromisoformat(payload["last_message_at"].replace("Z", "+00:00"))
        except ValueError:
            payload["last_message_at"] = None
    db.upsert_chat(payload)
    return OkOut(ok=True)


@app.get("/chats", response_model=List[ChatOut], dependencies=[Depends(verify_api_key)])
def get_chats(limit: int = Query(default=100, ge=1, le=500)) -> List[ChatOut]:
    rows = db.list_chats(limit=limit)
    return [_chat_to_out(r) for r in rows]


@app.post("/send", response_model=SendOut, dependencies=[Depends(verify_api_key)])
def post_send(item: SendIn) -> SendOut:
    item_id = db.enqueue_send(item.model_dump())
    with db.session_scope() as s:
        row = s.get(db.SendQueue, item_id)
        s.expunge(row)
        return _send_to_out(row)


@app.get("/send/next", response_model=Optional[SendOut], dependencies=[Depends(verify_api_key)])
def get_next_send() -> Optional[SendOut]:
    row = db.claim_next_send()
    if not row:
        return None
    return _send_to_out(row)


@app.post("/send/{item_id}/finish", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def finish_send(item_id: int, ok: bool = True, error: Optional[str] = None) -> OkOut:
    db.finish_send(item_id, ok=ok, error=error)
    return OkOut(ok=True)


@app.get("/send/{item_id}", response_model=Optional[SendOut], dependencies=[Depends(verify_api_key)])
def get_send(item_id: int) -> Optional[SendOut]:
    with db.session_scope() as s:
        row = s.get(db.SendQueue, item_id)
        if not row:
            return None
        s.expunge(row)
        return _send_to_out(row)


@app.get("/status", response_model=StatusOut, dependencies=[Depends(verify_api_key)])
def get_status() -> StatusOut:
    return StatusOut(
        auth=db.get_auth_state(),
        queue=db.queue_stats(),
        undelivered=len(db.list_undelivered_events(limit=1000)),
        chats=len(db.list_chats(limit=1000)),
    )


# ---------- Auth state & 2FA/пароль от владельца (вводятся через Telegram-бота) ----------


class AuthStateIn(BaseModel):
    status: str
    error: Optional[str] = None
    # Если True — ``last_error`` сбрасывается в NULL даже при ``error=None``.
    # Нужно max-процессу, чтобы «очистить» предыдущую ошибку при status=ok
    # или при ручном reauth (если раньше была rate-limit-ошибка).
    clear_error: bool = False


@app.post("/auth/state", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def post_auth_state(body: AuthStateIn) -> OkOut:
    db.set_auth_state(
        body.status,
        error=body.error,
        last_login=body.status == "ok",
        clear_error=body.clear_error,
    )
    return OkOut(ok=True)


class TwoFaRequestIn(BaseModel):
    kind: str = "sms"  # "sms" | "password"


class TwoFaRequestOut(BaseModel):
    request_id: int
    kind: str


@app.post("/auth/2fa/request", response_model=TwoFaRequestOut, dependencies=[Depends(verify_api_key)])
def post_2fa_request(body: Optional[TwoFaRequestIn] = None) -> TwoFaRequestOut:
    """max-процесс вызывает, когда PyMax SmsAuthFlow запросил SMS-код или пароль.

    ``kind`` может быть ``"sms"`` (по умолчанию) или ``"password"`` — чтобы
    Telegram-бот отправлял владельцу разные подсказки.
    """
    kind = (body.kind if body else "sms") or "sms"
    if kind not in ("sms", "password"):
        kind = "sms"
    rid = db.open_2fa_request(kind=kind)
    logger.info("2fa request opened: id=%s kind=%s", rid, kind)
    return TwoFaRequestOut(request_id=rid, kind=kind)


class TwoFaCodeIn(BaseModel):
    request_id: int
    code: str


@app.post("/auth/2fa", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def post_2fa(body: TwoFaCodeIn) -> OkOut:
    """Telegram-бот кладёт сюда код/пароль, введённый владельцем."""
    db.put_2fa_code(body.request_id, body.code)
    return OkOut(ok=True)


class TwoFaCodeOut(BaseModel):
    code: Optional[str] = None


@app.get("/auth/2fa/peek/{request_id}", response_model=TwoFaCodeOut, dependencies=[Depends(verify_api_key)])
def peek_2fa(request_id: int) -> TwoFaCodeOut:
    """max-процесс опрашивает этот эндпоинт, чтобы забрать введённый код/пароль."""
    code = db.take_pending_2fa_code(request_id)
    if code is not None:
        db.clear_2fa_request()
    return TwoFaCodeOut(code=code)


# ---------- Команда авторизации от владельца (только по явному действию) ----------


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


@app.post("/auth/action", response_model=AuthActionOut, dependencies=[Depends(verify_api_key)])
def post_auth_action(body: AuthActionIn) -> AuthActionOut:
    action = (body.action or "").strip().lower()
    if action not in ("sms", "session", "cancel"):
        raise HTTPException(status_code=400, detail="action must be 'sms' | 'session' | 'cancel'")
    db.set_pending_action(action)
    logger.info("auth action queued: %s", action)
    return AuthActionOut(ok=True, pending_action=action)


# ---------- Одноразовое уведомление для бота (consume) ----------


class NotifyOut(BaseModel):
    ok: bool = True
    message: Optional[str] = None


@app.post("/auth/notify/consume", response_model=NotifyOut, dependencies=[Depends(verify_api_key)])
def post_auth_notify_consume() -> NotifyOut:
    """Забрать (и сбросить) одноразовое ``auth_state.notify_message``.

    AuthWatcher в боте вызывает этот эндпоинт после того, как переслал
    сообщение владельцу — иначе оно будет показываться на каждом тике
    (3 секунды). Без отдельного эндпоинта бот не может сбросить поле
    (SQLAlchemy-сессия живёт только в api-процессе).
    """
    msg = db.consume_notify_message()
    return NotifyOut(ok=True, message=msg)


# ---------- Загрузка session-файла MAX владельцем ----------


class SessionUploadOut(BaseModel):
    ok: bool = True
    path: str
    size: int


@app.post("/admin/session/upload", response_model=SessionUploadOut, dependencies=[Depends(verify_api_key)])
async def post_session_upload(file: UploadFile = File(...)) -> SessionUploadOut:
    """Владелец через бота загружает ``bridge.db`` (PyMax session).

    Файл сохраняется в ``CACHE_DIR/bridge.db``. ``.db-shm``/``.db-wal`` (если
    были) стираются, чтобы PyMax при следующем старте не подцепил старый WAL
    с устаревшими данными. Supervisor увидит файл через session-watcher и
    перейдёт в режим ``session_attached``, ожидая команды «Подключиться».
    """
    cache_dir = Path(settings.cache_dir)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"cache_dir not writable: {exc}")

    # Ограничим размер (50 МБ) — больше у PyMax всё равно не бывает.
    MAX_SIZE = 50 * 1024 * 1024
    target = cache_dir / "bridge.db"
    try:
        # Стираем старые sqlite-sidecar'ы (shm/wal), иначе PyMax может
        # прочитать устаревший WAL от прошлой сессии.
        for sidecar in (cache_dir / "bridge.db-shm", cache_dir / "bridge.db-wal"):
            if sidecar.exists():
                try:
                    sidecar.unlink()
                except OSError:
                    pass
        # Атомарная запись: пишем во временный файл, затем переименовываем.
        tmp_path = target.with_suffix(target.suffix + ".upload")
        written = 0
        with open(tmp_path, "wb") as f:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_SIZE:
                    f.close()
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                    raise HTTPException(status_code=413, detail="file too large (>50 MB)")
                f.write(chunk)
        os.replace(tmp_path, target)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"upload failed: {exc}")

    db.set_session_file_path(str(target))
    # Сообщим AuthWatcher'у, что надо показать inline-меню «Подключиться / Отменить».
    db.set_notify_message(
        f"📥 Принят session-файл ({written} байт). Сохранён в {target}. "
        "Можно подключаться."
    )
    # Автоматически переведём в session_attached, если ещё не в нём.
    state = db.get_auth_state()
    if state.get("status") in ("auth_required", "unknown", None):
        db.set_auth_state("session_attached", clear_error=True)
    logger.info("session uploaded: path=%s size=%d", target, written)
    return SessionUploadOut(ok=True, path=str(target), size=written)


# ---------- Session management endpoints ----------


class SessionInfo(BaseModel):
    name: str
    path: str
    size: int
    modified: float


class SessionListOut(BaseModel):
    sessions: List[SessionInfo]
    current: Optional[str] = None


@app.get("/admin/session/list", response_model=SessionListOut, dependencies=[Depends(verify_api_key)])
async def get_session_list() -> SessionListOut:
    """Возвращает список доступных session-файлов в кэш-директории.

    Ищет файлы с расширением .db и без расширения, которые могут быть
    сессиями PyMax (например, bridge.db, bridge, session1.db и т.п.).
    """
    cache_dir = Path(settings.cache_dir)
    sessions = []

    if not cache_dir.exists():
        return SessionListOut(sessions=[])

    # Ищем потенциальные session-файлы
    for pattern in ["*.db", "*"]:
        for path in cache_dir.glob(pattern):
            if path.is_file() and not path.name.endswith(('-shm', '-wal', '-journal')):
                try:
                    stat = path.stat()
                    sessions.append(SessionInfo(
                        name=path.name,
                        path=str(path),
                        size=stat.st_size,
                        modified=stat.st_mtime
                    ))
                except (OSError, IOError):
                    continue

    # Сортируем по времени модификации (новые сначала)
    sessions.sort(key=lambda x: x.modified, reverse=True)

    # Определяем текущий session-файл (тот, что указан в auth_state)
    current_path = None
    auth_state = db.get_auth_state()
    if auth_state.get("session_file_path"):
        current_path = auth_state["session_file_path"]

    return SessionListOut(sessions=sessions, current=current_path)


class SessionUseIn(BaseModel):
    session_name: str


@app.post("/admin/session/use", response_model=OkOut, dependencies=[Depends(verify_api_key)])
async def post_session_use(body: SessionUseIn) -> OkOut:
    """Копирует выбранный session-файл в bridge.db для использования.

    Позволяет владельцу выбрать один из доступных session-файлов и
    сделать его активным (скопировав в bridge.db), после чего можно
    будет подключиться через supervisor с действием "session".
    """
    cache_dir = Path(settings.cache_dir)
    source_path = cache_dir / body.session_name
    target_path = cache_dir / "bridge.db"

    # Проверяем, что файл существует и находится в кэш-директории
    if not source_path.is_file():
        raise HTTPException(status_code=404, detail=f"Session file not found: {body.session_name}")

    # Проверяем, что путь не выходит за пределы кэш-директории (защита от path traversal)
    try:
        source_path.resolve().relative_to(cache_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session file path")

    # Ограничиваем размер
    MAX_SIZE = 50 * 1024 * 1024
    try:
        file_size = source_path.stat().st_size
        if file_size > MAX_SIZE:
            raise HTTPException(status_code=413, detail="Session file too large (>50 MB)")
    except (OSError, IOError):
        raise HTTPException(status_code=400, detail="Cannot read session file size")

    try:
        # Стираем старые sidecar-файлы
        for sidecar in (target_path.with_suffix('.db-shm'), target_path.with_suffix('.db-wal')):
            if sidecar.exists():
                try:
                    sidecar.unlink()
                except OSError:
                    pass

        # Копируем файл
        shutil.copy2(source_path, target_path)

        # Обновляем информацию в БД
        db.set_session_file_path(str(target_path))
        db.set_notify_message(
            f"📂 Выбран session-файл: {body.session_name} ({file_size} байт). "
            "Скопирован в bridge.db. Можно подключаться."
        )

        # Переводим в session_attached, если ещё не в нём
        state = db.get_auth_state()
        if state.get("status") in ("auth_required", "unknown", None):
            db.set_auth_state("session_attached", clear_error=True)

        logger.info("session selected for use: %s -> bridge.db (%d bytes)",
                    body.session_name, file_size)
        return OkOut(ok=True)

    except Exception as exc:
        logger.warning("session use failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to use session: {exc}")


# ---------- Внутренние уведомления (опционально, для отладки) ----------


class NotifyIn(BaseModel):
    """Используется max-процессом, чтобы сообщить api о системных событиях
    (например, «пришёл запрос на SMS»). Сейчас api сам опрашивает auth_state,
    поэтому этот маршрут — no-op, оставлен на будущее (push вместо polling)."""

    event: str
    payload: Optional[dict] = None


@app.post("/internal/notify", dependencies=[Depends(verify_api_key)])
def internal_notify(body: NotifyIn) -> OkOut:
    logger.info("internal_notify: %s %s", body.event, body.payload)
    return OkOut(ok=True)
