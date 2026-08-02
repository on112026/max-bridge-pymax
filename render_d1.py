"""render_d1.py — клиент Cloudflare D1 REST API для max-bridge-pymax.

Хранит в D1 две вещи:
  1. Дамп основной SQLite-БД моста (bridge.db) — таблица meta_blobs, key='db'
  2. Файл сессии PyMax (CACHE_DIR/bridge.db) — таблица meta_blobs, key='session'

При рестарте Render:
  - скачивает оба blob'а из D1
  - пишет файлы на диск
  - приложение стартует без SMS и с сохранённым состоянием

Конфигурация (переменные окружения):
  CF_ACCOUNT_ID    — Account ID (Cloudflare dashboard → правый нижний угол)
  CF_API_TOKEN     — API Token (нужны права: D1:Edit для нужного аккаунта)
  CF_D1_DATABASE_ID — UUID базы данных D1

Использование как CLI:
  python render_d1.py init              # создать таблицу meta_blobs в D1
  python render_d1.py push-db          # залить bridge.db → D1
  python render_d1.py push-session     # залить сессию → D1
  python render_d1.py push-all         # оба файла сразу
  python render_d1.py pull-db          # скачать bridge.db из D1
  python render_d1.py pull-session     # скачать сессию из D1
  python render_d1.py pull-all         # оба файла (используется при старте)
  python render_d1.py status           # показать что лежит в D1

Или как модуль:
  from render_d1 import D1Store
  store = D1Store.from_env()
  store.push_blob("session", Path("/data/cache/bridge.db"))
  store.pull_blob("session", Path("/data/cache/bridge.db"))
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# D1 REST API base
_D1_API = "https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{db_id}"

# Таблица в D1, где хранятся бинарные блобы (db и session)
_INIT_SQL = """
CREATE TABLE IF NOT EXISTS meta_blobs (
    key       TEXT PRIMARY KEY,
    data      TEXT NOT NULL,
    size_kb   INTEGER,
    updated_at TEXT
);
"""


class D1Error(RuntimeError):
    pass


class D1Store:
    """Клиент для работы с D1 через REST API."""

    def __init__(self, account_id: str, api_token: str, db_id: str):
        self.account_id = account_id
        self.api_token = api_token
        self.db_id = db_id
        self._base = _D1_API.format(account_id=account_id, db_id=db_id)

    @classmethod
    def from_env(cls) -> "D1Store":
        """Создать из переменных окружения CF_ACCOUNT_ID, CF_API_TOKEN, CF_D1_DATABASE_ID."""
        account_id = os.environ.get("CF_ACCOUNT_ID", "")
        api_token = os.environ.get("CF_API_TOKEN", "")
        db_id = os.environ.get("CF_D1_DATABASE_ID", "")
        if not all([account_id, api_token, db_id]):
            raise D1Error(
                "Не заданы переменные окружения: CF_ACCOUNT_ID, CF_API_TOKEN, CF_D1_DATABASE_ID"
            )
        return cls(account_id, api_token, db_id)

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """Выполнить запрос к D1 REST API."""
        url = self._base + path
        data = json.dumps(body).encode() if body else None
        req = Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except HTTPError as e:
            body_text = e.read().decode(errors="replace")
            raise D1Error(f"D1 API HTTP {e.code}: {body_text}") from e
        except URLError as e:
            raise D1Error(f"D1 API сетевая ошибка: {e.reason}") from e

        if not result.get("success"):
            errors = result.get("errors", [])
            raise D1Error(f"D1 API вернул ошибку: {errors}")
        return result

    def query(self, sql: str, params: Optional[list] = None) -> list[dict]:
        """Выполнить SQL-запрос в D1, вернуть список строк."""
        body: dict = {"sql": sql}
        if params:
            body["params"] = params
        result = self._request("POST", "/query", body)
        # D1 /query возвращает массив результатов (по одному на запрос)
        results = result.get("result", [])
        if not results:
            return []
        return results[0].get("results", [])

    def execute(self, sql: str, params: Optional[list] = None) -> dict:
        """Выполнить DDL или DML запрос (без возврата строк)."""
        body: dict = {"sql": sql}
        if params:
            body["params"] = params
        return self._request("POST", "/query", body)

    # ---------- Инициализация ----------

    def init(self) -> None:
        """Создать таблицу meta_blobs если не существует."""
        self.execute(_INIT_SQL)
        logger.info("D1: таблица meta_blobs готова")

    # ---------- Блобы (бинарные файлы) ----------

    def push_blob(self, key: str, path: Path) -> bool:
        """Загрузить файл в D1 как base64-blob.

        Возвращает True при успехе.
        """
        if not path.exists():
            logger.warning("push_blob: файл не найден: %s", path)
            return False
        raw = path.read_bytes()
        encoded = base64.b64encode(raw).decode()
        size_kb = len(raw) // 1024
        updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.execute(
            """
            INSERT INTO meta_blobs (key, data, size_kb, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                data       = excluded.data,
                size_kb    = excluded.size_kb,
                updated_at = excluded.updated_at
            """,
            [key, encoded, size_kb, updated_at],
        )
        logger.info("D1 push_blob: key=%s, %d KB, path=%s", key, size_kb, path)
        return True

    def pull_blob(self, key: str, dest: Path) -> bool:
        """Скачать blob из D1 и записать в файл.

        Возвращает True если blob найден и записан, False если blob отсутствует.
        """
        rows = self.query(
            "SELECT data, size_kb, updated_at FROM meta_blobs WHERE key = ?", [key]
        )
        if not rows:
            logger.info("D1 pull_blob: key=%s не найден (первый запуск?)", key)
            return False
        row = rows[0]
        encoded = row["data"]
        raw = base64.b64decode(encoded)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        logger.info(
            "D1 pull_blob: key=%s → %s (%d KB, сохранено %s)",
            key, dest, row.get("size_kb", 0), row.get("updated_at", "?"),
        )
        return True

    def blob_info(self, key: str) -> Optional[dict]:
        """Вернуть метаданные blob'а (size_kb, updated_at) или None."""
        rows = self.query(
            "SELECT size_kb, updated_at FROM meta_blobs WHERE key = ?", [key]
        )
        return rows[0] if rows else None

    # ---------- SQLite-дамп через online backup ----------

    def push_db(self, db_path: Path, key: str = "db") -> bool:
        """Сделать online-бэкап SQLite и загрузить в D1.

        Использует sqlite3.backup() — безопасно при работающем приложении,
        не блокирует транзакции.
        """
        if not db_path.exists():
            logger.warning("push_db: файл не найден: %s", db_path)
            return False
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            src = sqlite3.connect(str(db_path))
            dst = sqlite3.connect(str(tmp_path))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            return self.push_blob(key, tmp_path)
        except Exception as e:
            logger.error("push_db: ошибка: %s", e)
            return False
        finally:
            tmp_path.unlink(missing_ok=True)

    def pull_db(self, db_path: Path, key: str = "db") -> bool:
        """Скачать SQLite-дамп из D1 и записать на диск."""
        return self.pull_blob(key, db_path)

    # ---------- Статус ----------

    def status(self) -> dict:
        """Вернуть статус всех blob'ов в D1."""
        rows = self.query("SELECT key, size_kb, updated_at FROM meta_blobs ORDER BY key")
        return {r["key"]: {"size_kb": r["size_kb"], "updated_at": r["updated_at"]} for r in rows}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _require_env() -> D1Store:
    try:
        return D1Store.from_env()
    except D1Error as e:
        print(f"Ошибка: {e}")
        sys.exit(1)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    db_path = Path(os.environ.get("DB_PATH", "/data/bridge.db"))
    cache_dir = Path(os.environ.get("CACHE_DIR", "/data/cache"))
    session_path = cache_dir / "bridge.db"

    if cmd == "init":
        store = _require_env()
        store.init()
        print("✅ D1: таблица meta_blobs создана")

    elif cmd == "push-db":
        store = _require_env()
        ok = store.push_db(db_path)
        print(f"{'✅' if ok else '❌'} push-db: {db_path}")

    elif cmd == "push-session":
        store = _require_env()
        ok = store.push_blob("session", session_path)
        print(f"{'✅' if ok else '❌'} push-session: {session_path}")

    elif cmd == "push-all":
        store = _require_env()
        store.init()
        ok1 = store.push_db(db_path)
        ok2 = store.push_blob("session", session_path)
        print(f"{'✅' if ok1 else '❌'} push-db: {db_path}")
        print(f"{'✅' if ok2 else '❌'} push-session: {session_path}")

    elif cmd == "pull-db":
        store = _require_env()
        ok = store.pull_db(db_path)
        print(f"{'✅' if ok else '⚠️  не найдено'} pull-db: {db_path}")

    elif cmd == "pull-session":
        store = _require_env()
        ok = store.pull_blob("session", session_path)
        print(f"{'✅' if ok else '⚠️  не найдено'} pull-session: {session_path}")

    elif cmd == "pull-all":
        store = _require_env()
        ok1 = store.pull_db(db_path)
        ok2 = store.pull_blob("session", session_path)
        print(f"{'✅' if ok1 else '⚠️  не найдено'} pull-db: {db_path}")
        print(f"{'✅' if ok2 else '⚠️  не найдено'} pull-session: {session_path}")

    elif cmd == "status":
        store = _require_env()
        info = store.status()
        if not info:
            print("D1 meta_blobs: пусто (таблица не создана или данных нет)")
        else:
            print("D1 meta_blobs:")
            for key, meta in info.items():
                print(f"  {key:12s}  {meta['size_kb']:6d} KB  обновлено {meta['updated_at']}")

    else:
        print(__doc__)
        sys.exit(0 if cmd == "help" else 1)


if __name__ == "__main__":
    main()
