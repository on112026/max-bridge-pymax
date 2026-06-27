"""Управление кэш-директорией PyMax (``CACHE_DIR/bridge.db`` и др.).

Используется supervisor'ом в двух сценариях:

* ``wipe_cache`` — перед SMS-авторизацией (``pending_action="sms"``)
  стираем всю сессию, чтобы PyMax запросил новый код.
* ``find_session_files`` — ищем существующие session-файлы (для
  ``pending_action="session"`` и для ``_watch_session_file``).
* ``_watch_session_file`` — фоновая задача: следит за появлением
  session-файла на диске и переводит ``auth_state`` в ``session_attached``
  (даже если владелец положил файл руками через ``scp``/SSH).
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Optional

from shared import db as shared_db

from app.supervisor._backoff import (
    SESSION_WATCH_INTERVAL,
    sleep_with_stop,
)

logger = logging.getLogger(__name__)


def wipe_cache(cache_dir: str) -> None:
    """Стирает session (PyMax) в cache_dir, оставляя структуру каталога.

    Вызывается supervisor'ом перед ``pending_action="sms"`` — иначе PyMax
    может подцепить старую сессию и не запросить SMS.
    """
    p = Path(cache_dir)
    if not p.exists():
        return
    for entry in p.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    logger.warning("cache wiped: %s", cache_dir)


def find_session_files(cache_dir: str) -> list[Path]:
    """Ищет файлы-кандидаты на PyMax session в ``cache_dir``.

    Учитывает:

    * ``bridge`` (без расширения) — PyMax при ``session_name="bridge"``
      пишет сюда;
    * ``bridge.db`` — используется в API-эндпоинтах;
    * любые другие ``*.db`` — владелец мог положить session с
      произвольным именем.

    Возвращает список существующих файлов (исключая ``-shm``/``-wal``
    sidecar'ы), отсортированный по mtime (свежие первыми).
    """
    p = Path(cache_dir)
    if not p.is_dir():
        return []
    candidates: list[Path] = []
    seen: set[Path] = set()
    for name in ("bridge", "bridge.db"):
        f = p / name
        if f.is_file() and f not in seen:
            candidates.append(f)
            seen.add(f)
    for cand in p.glob("*.db"):
        if cand.name.endswith(("-shm", "-wal", "-journal")):
            continue
        if cand in seen:
            continue
        if cand.is_file():
            candidates.append(cand)
            seen.add(cand)
    candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return candidates


async def watch_session_file(
    stop_event,
    cache_dir: str,
    phone: str,
) -> None:
    """Следит за появлением/обновлением session-файла PyMax на диске.

    PyMax сохраняет сессию в ``cache_dir/<session_name>.db`` (``bridge.db``
    по умолчанию в нашем Client'е). Как только файл создан или его mtime
    свежее момента старта Client — считаем, что мост авторизован, и
    переводим ``auth_state`` в ``ok`` с очисткой ``last_error``.

    Зачем это нужно: ``pymax.client.start()`` — fire-and-forget, но PyMax
    завершает корутину ``start()`` сразу после первого ``client started``.
    ``client_task.done() == True`` без исключения, и supervisor не
    понимает, что мост живой — он пытается пересоздать Client, упирается
    в ``sms-cooldown`` и сбрасывает сессию. Здесь мы сами выставляем
    ``status=ok`` по факту появления session.db на диске.

    В режиме «только по команде» watcher НЕ должен ставить ``ok`` если
    владелец ещё не дал команду (``auth_required``) или идёт процесс
    SMS/2FA (``need_2fa``) или MAX под rate-limit (``rate_limited``).
    Иначе мы заспойлим владельцу ложное «✅ MAX: вход выполнен успешно»
    пока он только положил файл сессии в кэш и ещё не подтвердил.

    Дополнительно: если на диске нашёлся ``.db`` с произвольным именем
    (владелец положил руками), и в ``auth_state.session_file_path`` пусто —
    выставляем ``session_attached`` и прописываем путь, чтобы бот
    AuthWatcher прислал inline-меню «📂 Подключиться по сессии».
    """
    logger.info(
        "session watcher started (cache=%s, poll=%.1fs)",
        cache_dir, SESSION_WATCH_INTERVAL,
    )
    started_at = time.time()
    last_posted_ok = False
    last_announced_path: Optional[str] = None
    while not stop_event.is_set():
        try:
            candidates = find_session_files(cache_dir)
            if candidates:
                # Берём самый свежий (первый после сортировки).
                session_path = candidates[0]
                mtime = session_path.stat().st_mtime
                current = shared_db.get_auth_state()
                current_status = current.get("status") or "unknown"
                current_sf = current.get("session_file_path")

                # Если в БД путь не зафиксирован — прописываем и
                # переводим в session_attached (даже если status уже
                # auth_required), чтобы владелец увидел кнопку «📂 Подключиться».
                if current_sf != str(session_path):
                    if current_status in ("auth_required", "unknown", None):
                        logger.info(
                            "session watcher: detected %s, switching to session_attached",
                            session_path,
                        )
                        shared_db.set_session_file_path(str(session_path))
                        shared_db.set_auth_state(
                            "session_attached", error=None, clear_error=True
                        )
                        shared_db.set_notify_message(
                            f"📥 На сервере обнаружен session-файл: "
                            f"<code>{session_path.name}</code>. "
                            f"Нажмите «📂 Подключиться по сессии» в меню авторизации."
                        )
                        last_posted_ok = False
                        last_announced_path = str(session_path)
                        continue

                # ВАЖНО: watcher НЕ должен автоматически выставлять ``status=ok`` —
                # это прерогатива только ``_on_start`` (см. bridge.py), который
                # срабатывает при реальном успешном старте Client'а. Иначе
                # supervisor при следующей итерации видит ``status=ok`` и считает,
                # что Client жив — пропускает ``consume_pending_action``, и мост
                # никогда не поднимается (классическая ловушка "fake ok").

                # Старый код с ``mtime < started_at - 5.0`` / ``set_auth_state("ok")``
                # удалён — он и был причиной регрессии, когда supervisor не
                # поднимал Client после рестарта, но в БД уже стоял ``status=ok``.
                _ = (mtime, started_at, current_status)  # keep references for linters
                last_posted_ok = False
        except Exception as exc:
            logger.warning("session watcher error: %s", exc)
        await sleep_with_stop(stop_event, SESSION_WATCH_INTERVAL)