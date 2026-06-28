"""FSM-состояния для chat-операций MAX (этап 2).

Сейчас здесь только :class:`InviteUserState` для сценария
``/invite <chat_id> <+79...>``: бот ставит задачу ``search_user``
в очередь и ждёт результата. Сам по себе результат приходит
синхронно (через polling ``GET /chat_ops/{id}?wait=true``), без
следующего шага от пользователя — но состояние нужно, чтобы
корректно отрабатывать ``state.clear()`` при ошибке поиска,
отмене через inline-кнопку «❌ Отмена» или таймауте.

Если появятся другие многошаговые сценарии (например,
``/resolve`` → выбор чата → ``/join``), их FSM-группы стоит
класть сюда же, чтобы не разрастался общий ``states.py``.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class InviteUserState(StatesGroup):
    """Состояние владельца после ``/invite <chat_id> <+79...>``.

    * ``waiting_phone`` — бот поставил задачу ``search_user`` в очередь
      (``POST /chat_ops/search_user``), ждёт результата через
      ``api.wait_chat_op(...)``. На этом шаге пользователь может
      нажать inline-кнопку «❌ Отмена» (``chat_op:invite_cancel``)
      или «✅ Пригласить» (``chat_op:invite_confirm:<chat>:<uid>``),
      после чего состояние сбрасывается через ``state.clear()``.
    """

    waiting_phone = State()