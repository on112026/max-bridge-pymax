"""Текстовые команды авторизации MAX: ``/reauth_sms`` и ``/code``.

В новой модели авторизации «только по команде» эти команды **не стирают
сессию сами** — они только кладут ``pending_action`` в БД или код
в ``system_state``. Supervisor при следующей итерации обрабатывает.

Поток:

* ``/reauth_sms`` → ``POST /auth/action {"action": "sms"}`` →
  supervisor стирает cache, поднимает Client с ``SmsAuthFlow``,
  тот запрашивает SMS-код через ``/auth/2fa/request`` → бот видит
  ``pending_2fa_request_id`` и просит владельца прислать ``/code``.

* ``/code <число>`` → ``POST /auth/2fa`` → ``system_state.2fa_code:<rid>``
  → ``_drain_2fa_codes_loop`` будит ``asyncio.Event`` провайдера →
  ``QueueSmsCodeProvider.get_code()`` забирает код и логинит MAX.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import types
from aiogram.fsm.context import FSMContext

from app.api_client import api
from app.handlers._common import _is_allowed, _reject
from app.states import ReauthSmsState

logger = logging.getLogger(__name__)


async def reauth_sms_command(message: types.Message, state: FSMContext) -> None:
    """``/reauth_sms`` — запросить SMS-авторизацию MAX через supervisor.

    Эта команда не стирает сессию сама — она только кладёт
    ``pending_action="sms"`` в БД. Supervisor на следующей итерации
    стирает cache, поднимает Client с SmsAuthFlow и т.д.
    """
    if not _is_allowed(message.from_user.id):
        return await _reject(message)

    logger.info("reauth_sms requested by uid=%s", message.from_user.id)
    await message.answer(
        "🔐 Запрашиваю у MAX SMS-авторизацию…\n"
        "• Если в кэше есть живая сессия — она будет стёрта.\n"
        "• Как только MAX пришлёт SMS или попросит пароль — пришлю уведомление.\n"
        "• Введите код или пароль командой /code <число>.\n"
        "• Отменить можно кнопкой «⛔ Отмена» в сообщении с меню."
    )

    try:
        await api.post_auth_action("sms")
    except Exception as exc:
        logger.warning("post_auth_action(sms) failed: %s", exc)
        await message.answer(f"⚠️ Не удалось передать команду API: {exc}")
        return

    await message.answer(
        "📨 Команда отправлена в supervisor. Жду ответа MAX (обычно 5–30 секунд)."
    )
    await state.set_state(ReauthSmsState.waiting_code)


async def code_command(message: types.Message, state: FSMContext) -> None:
    """``/code <число>`` — ввести SMS-код или 2FA-пароль для текущего pending 2FA.

    Логика совпадает с прежней версией, но обновлена под новый auth-флоу:
    если ``status=auth_required`` без pending rid — просим владельца сначала
    нажать ``/reauth_sms`` (а не ``/code`` сразу).
    """
    if not _is_allowed(message.from_user.id):
        return await _reject(message)
    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Использование: /code <число>")
        return
    code = args[1].strip()
    if not code or len(code) < 4:
        await message.answer("Код слишком короткий.")
        return

    logger.info(
        "/code received from uid=%s code_len=%d", message.from_user.id, len(code)
    )

    try:
        s = await api.status()
    except Exception as exc:
        logger.warning("code_command: api.status() failed: %s", exc)
        await message.answer(f"⚠️ API: {exc}")
        return
    auth = s.get("auth") or {}
    rid = auth.get("pending_2fa_request_id")
    pending_kind = (auth.get("pending_2fa_kind") or "").lower() or "?"
    last_2fa_at = auth.get("last_2fa_request_at")
    status = auth.get("status")

    if not rid:
        recent = False
        if last_2fa_at:
            try:
                ts_raw = last_2fa_at
                if isinstance(ts_raw, str):
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                else:
                    ts = ts_raw
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                recent = (datetime.now(timezone.utc) - ts) < timedelta(minutes=10)
            except Exception:
                recent = False

        if status == "ok":
            await message.answer(
                "✅ MAX уже залогинен. Код не нужен. Если сообщения не приходят — /status."
            )
        elif status == "auth_required":
            await message.answer(
                "🔐 MAX сейчас ожидает вашу команду.\n"
                "Нажмите /reauth_sms, чтобы начать SMS-авторизацию,\n"
                "или «📂 Загрузить сессию MAX», чтобы загрузить файл."
            )
        elif recent:
            await message.answer(
                "⏳ MAX ещё не успел зарегистрировать новый запрос кода. "
                "Подождите ~30 секунд и пришлите /code ещё раз."
            )
        else:
            await message.answer(
                "Сейчас MAX не запрашивает код. Возможно, сессия ещё жива — попробуйте позже. "
                "Если это после /reauth_sms — подождите 30 секунд и пришлите /code ещё раз."
            )
        return

    try:
        await api.put_2fa(request_id=rid, code=code)
        logger.info(
            "/code forwarded to api: rid=%s uid=%s code_len=%d kind=%s",
            rid, message.from_user.id, len(code), pending_kind,
        )
        kind_label = {
            "sms": "SMS-код",
            "password": "2FA-пароль",
        }.get(pending_kind, "код")
        await message.answer(
            f"✅ {kind_label} отправлен (request_id={rid}, kind={pending_kind}). "
            "Дождитесь логина MAX."
        )
    except Exception as exc:
        logger.warning("code_command: put_2fa failed rid=%s: %s", rid, exc)
        await message.answer(f"⚠️ Не удалось передать код: {exc}")
        return
    await state.clear()