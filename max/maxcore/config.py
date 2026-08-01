"""Загрузка настроек max-процесса (этап 2, PyMax)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "max"))

from shared.config import load_settings  # noqa: E402

settings = load_settings()