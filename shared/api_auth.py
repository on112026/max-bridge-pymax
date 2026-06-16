"""FastAPI-зависимость для проверки API-ключа.

Импортируется ТОЛЬКО api-сервисом. HTTP-клиент для вызова API
живёт в ``shared.http_client`` и не зависит от FastAPI, чтобы
его могли использовать бот и watcher.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import Header, HTTPException, status


async def verify_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    expected = os.getenv("BRIDGE_API_KEY", "")
    if not expected:
        # Если ключ не задан на стороне API, откажем всем
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API key not configured")
    if not x_api_key or x_api_key != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")