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