"""Роутер управления session-файлами MAX (для бота).

Эндпоинты:

* ``POST /admin/session/upload`` — бот загружает ``bridge.db`` (PyMax session)
  через multipart/form-data. Файл сохраняется в ``CACHE_DIR/bridge.db``,
  ``.db-shm``/``.db-wal`` стираются, ``auth_state`` переводится в
  ``session_attached``.
* ``GET /admin/session/list`` — бот показывает список доступных
  session-файлов в кэш-директории (``/sessions``).
* ``POST /admin/session/use`` — бот выбирает конкретный session-файл
  (копирует в ``bridge.db``).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from shared import db
from shared.api_auth import verify_api_key
from shared.config import load_settings

from api.routers.schemas import (
    OkOut,
    SessionInfo,
    SessionListOut,
    SessionUploadOut,
    SessionUseIn,
)

logger = __import__("logging").getLogger(__name__)
router = APIRouter()

settings = load_settings()


@router.post("/admin/session/upload", response_model=SessionUploadOut, dependencies=[Depends(verify_api_key)])
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


@router.get("/admin/session/list", response_model=SessionListOut, dependencies=[Depends(verify_api_key)])
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


@router.post("/admin/session/use", response_model=OkOut, dependencies=[Depends(verify_api_key)])
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