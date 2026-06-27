"""Функции для работы с таблицей ``auth_state`` — состояние авторизации MAX.

Модель «только по команде» (см. ``max/app/supervisor.py::run``):

* На cold-start ``status=auth_required``, supervisor НЕ поднимает Client.
* Владелец через бота жмёт inline-кнопку → ``pending_action`` кладётся
  через ``set_pending_action``.
* Supervisor на следующей итерации забирает ``consume_pending_action``
  и обрабатывает (``sms`` / ``session`` / ``cancel``).
* При status=ok Client продолжает работать; supervisor не пересоздаёт
  его без причины.

``notify_message`` — одноразовое сообщение от supervisor'а для бота
(например, «session uploaded, size=12345»). После показа AuthWatcher
вызывает ``consume_notify_message`` и сбрасывает.

``pending_2fa_*`` — данные о текущем pending 2FA-запросе (SMS или
2FA-пароль). ``open_2fa_request`` создаёт новый запрос, ``take_pending_2fa_code``
забирает код (если владелец ввёл через ``/code``).
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import select

from shared.db._engine import session_scope
from shared.db._models import AuthState, SystemState


# ---------- Базовые операции над auth_state ----------


def get_auth_state() -> dict:
    """Словарь со всеми полями ``auth_state`` для ``GET /status``."""
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
        start() или после ручного reauth).
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


# ---------- 2FA-запросы (SMS / 2FA-пароль) ----------


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
    """Забрать 2FA-код из ``SystemState`` (владелец ввёл через ``/code``)."""
    key = f"2fa_code:{request_id}"
    with session_scope() as s:
        row = s.query(SystemState).filter(SystemState.key == key).first()
        if not row:
            return None
        code = row.value
        s.delete(row)
        return code


def put_2fa_code(request_id: int, code: str) -> None:
    """Положить 2FA-код в ``SystemState`` (от бота через ``/code``)."""
    key = f"2fa_code:{request_id}"
    with session_scope() as s:
        row = s.query(SystemState).filter(SystemState.key == key).first()
        if row:
            row.value = code
            row.updated_at = datetime.utcnow()
        else:
            s.add(SystemState(key=key, value=code))


def list_2fa_code_keys() -> List[int]:
    """Список ``request_id``, для которых владелец положил код/пароль.

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
    """Сбросить ``pending_2fa_*`` после успешного ввода кода."""
    with session_scope() as s:
        row = s.query(AuthState).first()
        if not row:
            return
        row.pending_2fa_request_id = None
        row.pending_2fa_kind = None
        row.updated_at = datetime.utcnow()