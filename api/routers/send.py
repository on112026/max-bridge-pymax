"""Роутер очереди отправки в MAX: ``POST/GET /send``.

Жизненный цикл задачи:

1. Бот кладёт задачу через ``POST /send`` (``enqueue_send``).
2. MAX-процесс забирает ``GET /send/next`` (``claim_next_send``),
   переводит в ``in_progress`` и шлёт через ``client.send_message``.
3. MAX-процесс сообщает о результате через ``POST /send/{id}/finish``.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from shared import db
from shared.api_auth import verify_api_key

from api.routers.schemas import OkOut, SendIn, SendOut

router = APIRouter()


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
        # ``tg_chat_id`` / ``tg_message_id`` — id TG-сообщения, из которого
        # ушёл ответ в MAX. Используется MAX-процессом для создания
        # ``DeliveredMessage``-строки после ``client.send_message`` (иначе
        # мост MAX→TG-реакций не сможет найти наше TG-сообщение и логирует
        # «DIALOG-mirror skip, no DeliveredMessage»).
        tg_chat_id=s.tg_chat_id,
        tg_message_id=s.tg_message_id,
    )


@router.post("/send", response_model=SendOut, dependencies=[Depends(verify_api_key)])
def post_send(item: SendIn) -> SendOut:
    item_id = db.enqueue_send(item.model_dump())
    with db.session_scope() as s:
        row = s.get(db.SendQueue, item_id)
        s.expunge(row)
        return _send_to_out(row)


@router.get("/send/next", response_model=Optional[SendOut], dependencies=[Depends(verify_api_key)])
def get_next_send() -> Optional[SendOut]:
    row = db.claim_next_send()
    if not row:
        return None
    return _send_to_out(row)


@router.post("/send/{item_id}/finish", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def finish_send(item_id: int, ok: bool = True, error: Optional[str] = None) -> OkOut:
    db.finish_send(item_id, ok=ok, error=error)
    return OkOut(ok=True)


@router.get("/send/{item_id}", response_model=Optional[SendOut], dependencies=[Depends(verify_api_key)])
def get_send(item_id: int) -> Optional[SendOut]:
    with db.session_scope() as s:
        row = s.get(db.SendQueue, item_id)
        if not row:
            return None
        s.expunge(row)
        return _send_to_out(row)