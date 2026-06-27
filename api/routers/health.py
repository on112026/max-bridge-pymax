"""Базовые healthcheck-эндпоинты для Railway/Render/etc.

``GET /health`` — для liveness/readiness-проб. Возвращает ``{"status": "ok"}``
без обращения к БД (это важно — если БД залочена, healthcheck всё равно
должен отвечать 200).

``GET /`` — корневой эндпоинт с версией сервиса, для удобства отладки
(``curl http://localhost:8000/``).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict:
    """Healthcheck (без обращения к БД)."""
    return {"status": "ok"}


@router.get("/")
def root() -> dict:
    """Корневой эндпоинт с версией сервиса."""
    return {"service": "max-bridge-pymax-api", "version": "2.0.0"}