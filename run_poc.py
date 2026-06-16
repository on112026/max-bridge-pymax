"""PoC: минимальный клиент PyMax.

Цель — проверить, что PyMax вообще авторизуется в вашем MAX-аккаунте
и присылает входящие сообщения.

Запуск:
    cd max-bridge-pymax
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .envOLD.example .envOLD  # отредактируйте MAX_PHONE
    python run_poc.py

Что произойдёт:
  1. Поднимется pymax.Client с work_dir=./cache.
  2. Если ./cache/main.db нет — PyMax запросит SMS-код через консоль
     (используется встроенный ConsoleSmsCodeProvider).
  3. Если MAX попросит 2FA — введите пароль в консоль
     (ConsolePasswordProvider).
  4. После логина в консоль будут печататься все входящие сообщения.
  5. Чтобы остановить — Ctrl+C.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# Подключаем vendor/pymax как обычный пакет.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))

from dotenv import load_dotenv  # noqa: E402

from pymax import Client, Message  # noqa: E402

load_dotenv(ROOT / ".envOLD")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
# Подкручиваем логгер самого PyMax, чтобы видеть handshake/login.
logging.getLogger("pymax").setLevel(LOG_LEVEL)

logger = logging.getLogger("poc")


def _short(value: object, limit: int = 80) -> str:
    s = str(value) if value is not None else ""
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _print_message(message: Message) -> None:
    sender = message.sender
    sender_name = getattr(sender, "name", None) if sender else None
    attaches = message.attaches or []
    kinds = [type(a).__name__ for a in attaches]
    print(
        "\n--- INCOMING ---"
        f"\n  chat_id:   {message.chat_id}"
        f"\n  message_id:{message.id}"
        f"\n  sender:    {sender_name}"
        f"\n  text:      {_short(message.text)!r}"
        f"\n  attaches:  {kinds or '[]'}"
        "\n----------------"
    )


async def main() -> None:
    phone = os.getenv("MAX_PHONE", "").strip()
    if not phone:
        sys.exit("MAX_PHONE пуст — заполните .envOLD (см. .envOLD.example)")

    cache_dir = ROOT / "cache"
    cache_dir.mkdir(exist_ok=True)

    # PyMax сам подберёт ConsoleSmsCodeProvider / ConsolePasswordProvider,
    # если не передавать кастомные. Этого достаточно для PoC.
    client = Client(
        phone=phone,
        work_dir=str(cache_dir),
        session_name="main.db",
    )

    @client.on_start()
    async def on_start(_client: Client) -> None:
        me = _client.me
        me_id = getattr(getattr(me, "contact", None), "id", None) if me else None
        chats = _client.chats or []
        logger.info("on_start: me=%s chats=%d", me_id, len(chats))
        print(
            f"\n[OK] PyMax залогинился в MAX.\n"
            f"     me = {me_id}\n"
            f"     чатов в первом sync: {len(chats)}\n"
            f"     ждём входящие сообщения… (Ctrl+C для выхода)\n"
        )

    @client.on_message()
    async def on_message(message: Message, _client: Client) -> None:
        if message.chat_id is None:
            return
        _print_message(message)

    logger.info("Запускаем PyMax (work_dir=%s)", cache_dir)
    try:
        await client.start()
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем")
    except Exception:
        logger.exception("PyMax упал")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass