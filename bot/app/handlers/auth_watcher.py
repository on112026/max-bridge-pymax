"""AuthWatcher — фоновая задача в bot-процессе.

Раз в несколько секунд опрашивает ``/status`` и присылает владельцу
соответствующие уведомления:

* ``status=ok`` → «✅ MAX: вход выполнен успешно».
* ``status=need_2fa`` → «🔐 MAX прислал SMS-код» (или 2FA-пароль),
  просит прислать ``/code``.
* ``status=rate_limited`` → «⚠️ MAX временно ограничил авторизацию».
* ``status=auth_required`` → inline-меню «Что делать?» (SMS / сессия /
  загрузка / отмена).
* ``status=session_attached`` → inline-меню «📂 Подключиться по сессии».

Параллельно потребляет ``notify_message`` — если max-процесс положил
одноразовое сообщение (например «session uploaded, size=...»), AuthWatcher
показывает его владельцу и сбрасывает через ``POST /auth/notify/consume``.

Дополнительно: при ``status=ok`` + ``undelivered > 0`` + нет привязанной
supergroup — один раз напоминает владельцу сделать ``/setup``. Чтобы не
спамить, держит ``_supergroup_prompted_for: set[int]`` — сбрасывается
в ``_attach_supergroup_for_owner`` после успешной привязки.

Module-level реестр ``_active_auth_watcher: dict[int, AuthWatcher]``
нужен, чтобы ``_attach_supergroup_for_owner`` мог сбросить флаг
подсказки для текущего процесса (в проде он ровно один, но ключ-id
позволяет держать несколько watcher'ов в тестах).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from aiogram import Bot, types

from app.api_client import api
from app.config import settings
from app.handlers._common import _escape
from app.keyboards import auth_choice_keyboard
from shared import db as shared_db

logger = logging.getLogger(__name__)


# Module-level реестр текущего ``AuthWatcher`` (один на процесс).
# Нужен, чтобы ``_attach_supergroup_for_owner`` мог сбросить флаг
# подсказки «сделай /setup» при успешной привязке supergroup. Ключ — id,
# чтобы можно было держать несколько watcher'ов в тестах.
_active_auth_watcher: "dict[int, AuthWatcher]" = {}


class AuthWatcher:
    """Фоновая задача: опрашивает ``auth_state`` и присылает владельцу уведомления.

    Каждые ``POLL_INTERVAL`` секунд читает ``GET /status`` и реагирует
    на смену состояния:

    * ``status=ok`` → пуш «✅ MAX: вход выполнен успешно».
    * ``status=need_2fa`` → пуш с просьбой прислать ``/code``.
    * ``status=rate_limited`` → пуш с подсказкой про cooldown.
    * ``status=auth_required`` → пуш с inline-меню «Что делать?».
    * ``status=session_attached`` → пуш с inline-меню «📂 Подключиться».
    * потребляет ``notify_message`` (если max-процесс положил одноразовое
      сообщение, например «session uploaded, size=…») и пересылает его
      владельцу.
    """

    POLL_INTERVAL = 3.0

    API_ERROR_WARN_BUDGET = 3

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._notified_request_id: Optional[int] = None
        self._last_known_status: Optional[str] = None
        self._last_known_pending_action: Optional[str] = None
        self._last_known_session_path: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._api_error_count: int = 0
        # Множество owner_uid, для которых УЖЕ отправляли подсказку
        # «есть undelivered, но supergroup не подключена — сделай /setup».
        # Чтобы не спамить на каждом тике (3 секунды). Сбрасывается,
        # когда владелец успешно делает /setgroup или /autosetup.
        self._supergroup_prompted_for: set[int] = set()

    async def _notify_owner(self, text: str, reply_markup=None) -> None:
        for uid in settings.allowed_tg_user_ids:
            try:
                await self.bot.send_message(
                    uid, text, reply_markup=reply_markup
                )
            except Exception as exc:
                logger.warning("notify uid=%s failed: %s", uid, exc)

    @staticmethod
    def _prompt_text(kind: Optional[str]) -> str:
        if kind == "password":
            return (
                "🔐 MAX запросил <b>2FA-пароль</b>.\n"
                "Пришлите: <code>/code <ваш_пароль></code>"
            )
        return (
            "🔐 MAX прислал <b>SMS-код</b>.\n"
            "Посмотрите SMS на номер MAX и пришлите: <code>/code <число></code>"
        )

    @staticmethod
    def _has_session_on_disk(cache_dir: str) -> bool:
        """Локальная проверка на случай, если в auth_state.status ещё не
        успел обновиться, а session-файл уже залит напрямую на сервер.

        Проверяем не только ``bridge.db``, но и любой ``*.db`` в кэше
        (владелец мог положить файл с произвольным именем).
        """
        try:
            p = Path(cache_dir) / "bridge.db"
            if p.is_file():
                return True
            cache = Path(cache_dir)
            if not cache.is_dir():
                return False
            for cand in cache.glob("*.db"):
                if cand.is_file() and not cand.name.endswith(("-shm", "-wal")):
                    return True
            return False
        except Exception:
            return False

    def _session_present(self, auth: dict) -> bool:
        if auth.get("session_file_path"):
            return True
        # Фолбэк на cache_dir (владелец мог положить файл руками).
        return self._has_session_on_disk(settings.cache_dir)

    async def _tick(self) -> None:
        try:
            s = await api.status()
            self._api_error_count = 0
        except Exception as exc:
            self._api_error_count += 1
            if self._api_error_count <= self.API_ERROR_WARN_BUDGET:
                logger.warning(
                    "auth_watcher api error (%d/%d): %s",
                    self._api_error_count, self.API_ERROR_WARN_BUDGET, exc,
                )
            else:
                logger.debug("auth_watcher api error: %s", exc)
            return

        auth = s.get("auth") or {}
        status = auth.get("status") or "unknown"
        rid = auth.get("pending_2fa_request_id")
        kind = auth.get("pending_2fa_kind") or "sms"
        last_err = (auth.get("last_error") or "").lower()
        pending_action = auth.get("pending_action")
        session_path = auth.get("session_file_path")
        notify_message = auth.get("notify_message")

        # 1) Сначала потребляем одноразовое уведомление от supervisor'а —
        #    оно приоритетнее статусных сообщений, т.к. часто объясняет,
        #    что только что произошло (session uploaded, wipe и т.п.).
        if notify_message:
            self._last_consumed_notify = notify_message
            await self._notify_owner(notify_message)
            try:
                await api.consume_notify()
            except Exception as exc:
                logger.debug("consume_notify failed: %s", exc)
            return

        # 2) Реакция на смену основного статуса.
        if status != self._last_known_status:
            prev = self._last_known_status
            self._last_known_status = status

            if status == "ok":
                self._notified_request_id = None
                await self._notify_owner("✅ MAX: вход выполнен успешно.")
            elif status == "need_2fa":
                if rid and rid != self._notified_request_id:
                    self._notified_request_id = rid
                    await self._notify_owner(self._prompt_text(kind))
            elif status == "rate_limited":
                hint = ""
                if "limit.violate" in last_err or "rate" in last_err:
                    hint = " MAX ограничил частоту запросов — попробуем снова через ~10 мин."
                await self._notify_owner(
                    "⚠️ MAX временно ограничил авторизацию." + hint +
                    "\nКак только cooldown пройдёт, я пришлю уведомление."
                )
            elif status == "auth_required":
                # Главное меню — выбор способа авторизации.
                has_session = self._session_present(auth)
                kb = auth_choice_keyboard(
                    show_upload=not has_session,
                    show_session_connect=has_session,
                )
                await self._notify_owner(
                    "🔐 MAX не подключён.\n"
                    "• «🔐 SMS-авторизация» — стартовать новый Client и получить SMS.\n"
                    + (
                        "• «📂 Подключиться по сессии» — в кэше уже есть файл, попробуем его.\n"
                        if has_session else
                        "• «📎 Загрузить файл сессии» — сначала пришлите bridge.db.\n"
                    )
                    + "Выберите действие:",
                    reply_markup=kb,
                )
            elif status == "session_attached":
                # Владелец (или supervisor) обнаружил session-файл — ждём подтверждения.
                kb = auth_choice_keyboard(
                    show_upload=False,
                    show_session_connect=True,
                )
                path_disp = session_path or str(
                    Path(settings.cache_dir) / "bridge.db"
                )
                await self._notify_owner(
                    f"📥 В кэше MAX обнаружен session-файл:\n<code>{_escape(path_disp)}</code>\n"
                    "Нажмите «📂 Подключиться по сессии», чтобы войти.\n"
                    "Или «🔐 SMS-авторизация», чтобы войти по SMS (старая сессия будет стёрта).",
                    reply_markup=kb,
                )
            elif status == "unknown":
                if not rid:
                    self._notified_request_id = None
            # prev — только для возможного расширения логирования.
            _ = prev

        # 3) На случай, если status не менялся, но rid обновился.
        elif status == "need_2fa" and rid and rid != self._notified_request_id:
            self._notified_request_id = rid
            await self._notify_owner(self._prompt_text(kind))
        elif status == "unknown" and rid and rid != self._notified_request_id:
            self._notified_request_id = rid
            await self._notify_owner(self._prompt_text(kind))

        # 4) Если status=auth_required, а pending_action уже выставлен —
        #    дадим знать, что команда принята supervisor'ом (один раз на
        #    смену).
        if status == "auth_required" and pending_action and pending_action != self._last_known_pending_action:
            self._last_known_pending_action = pending_action
            label = {
                "sms": "📨 SMS-авторизация",
                "session": "🔌 Подключение по сессии",
                "cancel": "🛑 Отмена",
            }.get(pending_action, pending_action)
            await self._notify_owner(
                f"⏳ Команда «{label}» принята в очередь. Жду реакции supervisor'а."
            )
        elif not pending_action:
            self._last_known_pending_action = None

        # 5) Следим за появлением session-файла: если путь изменился —
        #    упомянем владельцу. Полноценное уведомление уйдёт через
        #    status=session_attached (выставляется /admin/session/upload).
        if session_path and session_path != self._last_known_session_path:
            self._last_known_session_path = session_path
            if status not in ("session_attached",):
                await self._notify_owner(
                    f"📂 Путь к session-файлу обновился: <code>{_escape(session_path)}</code>"
                )
        elif not session_path:
            self._last_known_session_path = None

        # 6) Подсказка про /setup: если MAX авторизован (status=ok), есть
        #    непрочитанные события из MAX, но владелец не подключил
        #    supergroup для пересылки — напоминаем один раз. Чтобы не
        #    спамить на каждом тике, держим set owner_uid, которым уже
        #    отправили подсказку. Setgroup / autosetup сбрасывают флаг.
        if status == "ok":
            undelivered = int(s.get("undelivered") or 0)
            if undelivered > 0:
                owner_uid = (
                    settings.allowed_tg_user_ids[0]
                    if settings.allowed_tg_user_ids else 0
                )
                if owner_uid and owner_uid not in self._supergroup_prompted_for:
                    try:
                        sg = shared_db.get_supergroup_for_owner(owner_uid)
                    except Exception as exc:
                        logger.debug(
                            "auth_watcher: get_supergroup_for_owner failed: %s", exc,
                        )
                        sg = None
                    if sg is None:
                        self._supergroup_prompted_for.add(owner_uid)
                        await self._notify_owner(
                            f"📬 У вас <b>{undelivered}</b> непрочитанных событий из MAX, "
                            "но бот пока не знает, в какую группу их пересылать.\n"
                            "Сделайте <code>/setup</code> — пришлю инструкцию "
                            "по созданию supergroup."
                        )
            else:
                # Нет непрочитанных — сбрасываем флаг, чтобы при следующем
                # всплеске событий снова напомнить.
                self._supergroup_prompted_for.clear()

    async def _run(self) -> None:
        logger.info("AuthWatcher started (poll=%.1fs)", self.POLL_INTERVAL)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("auth_watcher tick error: %s", exc)
            await asyncio.sleep(self.POLL_INTERVAL)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="auth-watcher")
            # Регистрируем себя в module-level реестре, чтобы
            # ``_attach_supergroup_for_owner`` мог сбросить флаг подсказки.
            _active_auth_watcher[id(self)] = self

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        _active_auth_watcher.pop(id(self), None)

    @staticmethod
    def get_active() -> "Optional[AuthWatcher]":
        """Возвращает текущий активный watcher (один на процесс) или ``None``.

        Используется ``_attach_supergroup_for_owner``, чтобы сбросить флаг
        «уже подсказали про /setup» после успешной привязки supergroup.
        """
        if not _active_auth_watcher:
            return None
        # Возвращаем последний запущенный (LIFO) — в проде он ровно один.
        return next(reversed(_active_auth_watcher.values()))