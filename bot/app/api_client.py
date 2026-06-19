async def upload_session_file(
        self,
        file_bytes: bytes,
        filename: str = "bridge.db",
        content_type: str = "application/octet-stream",
    ) -> Dict[str, Any]:
        """Отправить загруженный владельцем session-файл MAX в api.

        Файл сохраняется в ``CACHE_DIR/bridge.db`` (PyMax session), а
        ``auth_state`` переводится в ``session_attached`` — supervisor
        ждёт явной команды «Подключиться по сессии».
        """
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=120.0
        ) as c:
            r = await c.post(
                "/admin/session/upload",
                files={"file": (filename, file_bytes, content_type)},
                headers=self._client._headers(),
            )
            r.raise_for_status()
            return r.json() if r.content else {"ok": True}

    async def list_sessions(self) -> Dict[str, Any]:
        """Получить список доступных session-файлов из кэш-директории."""
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.get(
                "/admin/session/list",
                headers=self._client._headers(),
            )
            r.raise_for_status()
            return r.json() if r.content else {"sessions": []}

    async def use_session(self, session_name: str) -> Dict[str, Any]:
        """Выбрать session-файл для использования (скопировать в bridge.db)."""
        async with httpx.AsyncClient(
            base_url=self._client.base_url, timeout=10.0
        ) as c:
            r = await c.post(
                "/admin/session/use",
                json={"session_name": session_name},
                headers=self._client._headers(),
            )
            r.raise_for_status()
            return r.json() if r.content else {"ok": True}


api = BotApi()