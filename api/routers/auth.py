"""Роутер авторизации MAX (внутренний API для max- и bot-процессов).

Эндпоинты:

* ``POST /auth/state`` — max-процесс обновляет ``auth_state``.
* ``POST /auth/2fa/request`` — max-процесс открывает pending 2FA-запрос
  (SMS или пароль); бот через ``GET /status`` видит ``pending_2fa_request_id``
  и шлёт владельцу подсказку.
* ``POST /auth/2fa`` — бот кладёт код/пароль (``PUT``).
* ``GET /auth/2fa/peek/{rid}`` — max-процесс забирает введённый код
  и помечает запрос закрытым (``clear_2fa_request``).
* ``POST /auth/action`` — бот кладёт ``pending_action``
  (``sms``/``session``/``cancel``) для supervisor'а.
* ``POST /auth/notify/consume`` — бот сбрасывает ``auth_state.notify_message``
  после показа владельцу.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from shared import db
from shared.api_auth import verify_api_key

from api.routers.schemas import (
    AuthActionIn,
    AuthActionOut,
    AuthStateIn,
    NotifyOut,
    OkOut,
    TwoFaCodeIn,
    TwoFaCodeOut,
    TwoFaRequestIn,
    TwoFaRequestOut,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/auth/state", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def post_auth_state(body: AuthStateIn) -> OkOut:
    db.set_auth_state(
        body.status,
        error=body.error,
        last_login=body.status == "ok",
        clear_error=body.clear_error,
    )
    return OkOut(ok=True)


@router.post("/auth/2fa/request", response_model=TwoFaRequestOut, dependencies=[Depends(verify_api_key)])
def post_2fa_request(body: TwoFaRequestIn = None) -> TwoFaRequestOut:
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


@router.post("/auth/2fa", response_model=OkOut, dependencies=[Depends(verify_api_key)])
def post_2fa(body: TwoFaCodeIn) -> OkOut:
    """Telegram-бот кладёт сюда код/пароль, введённый владельцем."""
    db.put_2fa_code(body.request_id, body.code)
    return OkOut(ok=True)


@router.get("/auth/2fa/peek/{request_id}", response_model=TwoFaCodeOut, dependencies=[Depends(verify_api_key)])
def peek_2fa(request_id: int) -> TwoFaCodeOut:
    """max-процесс опрашивает этот эндпоинт, чтобы забрать введённый код/пароль."""
    code = db.take_pending_2fa_code(request_id)
    if code is not None:
        db.clear_2fa_request()
    return TwoFaCodeOut(code=code)


@router.post("/auth/action", response_model=AuthActionOut, dependencies=[Depends(verify_api_key)])
def post_auth_action(body: AuthActionIn) -> AuthActionOut:
    """Команда от бота (владельца) supervisor'у (sms/session/cancel)."""
    action = (body.action or "").strip().lower()
    if action not in ("sms", "session", "cancel"):
        raise HTTPException(status_code=400, detail="action must be 'sms' | 'session' | 'cancel'")
    db.set_pending_action(action)
    logger.info("auth action queued: %s", action)
    return AuthActionOut(ok=True, pending_action=action)


@router.post("/auth/notify/consume", response_model=NotifyOut, dependencies=[Depends(verify_api_key)])
def post_auth_notify_consume() -> NotifyOut:
    """Забрать (и сбросить) одноразовое ``auth_state.notify_message``.

    AuthWatcher в боте вызывает этот эндпоинт после того, как переслал
    сообщение владельцу — иначе оно будет показываться на каждом тике
    (3 секунды). Без отдельного эндпоинта бот не может сбросить поле
    (SQLAlchemy-сессия живёт только в api-процессе).
    """
    msg = db.consume_notify_message()
    return NotifyOut(ok=True, message=msg)