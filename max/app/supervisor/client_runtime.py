"""Создание и жизненный цикл PyMax ``Client``.

* ``build_client(phone, cache_dir)`` — конструирует ``Client`` с нашими
  ``QueueSmsCodeProvider`` / ``QueuePasswordProvider`` и регистрирует
  колбэки моста (``app.bridge.register_bridge``).
* ``_long_running_start(client, stop_event)`` — обёртка вокруг
  ``client.start()``, которая держит Client «живым» между чистыми
  завершениями ``start()`` (PyMax после первого ``client started`` и
  закрытия long-poll возвращается через ``else: return`` в
  ``pymax/base.py``).

В режиме «только по команде» (``supervisor.py::run``) supervisor сам
создаёт Client через ``build_client`` и оборачивает в
``_long_running_start``.
"""

from __future__ import annotations

import asyncio
import logging

from pymax import Client
from pymax.config import ExtraConfig
from pymax.auth.sms import SmsAuthFlow

from app.auth import QueuePasswordProvider, QueueSmsCodeProvider
from app.bridge import register_bridge

logger = logging.getLogger(__name__)


async def _long_running_start(client: Client, stop_event: asyncio.Event) -> None:
    """Обёртка вокруг ``client.start()``, которая держит Client живым.

    Проблема: ``pymax.base.BaseClient.start()`` после первой успешной
    синхронизации с MAX (``client started``) и закрытия long-poll соединения
    (``closing connection``) возвращается чисто через ``else: return``. Для
    supervisor'а это выглядит как «Client умер» — он пытается пересоздать
    Client, и если ``status`` ещё ``ok``, мы упираемся в гард и не
    пересоздаём. В итоге мост живёт, но новые события из MAX не
    обрабатываются, потому что ``start()`` уже завершился.

    Решение: после того, как ``client.start()`` вернулся без исключения,
    переинициализируем runtime и снова вызываем ``start()``. При обрыве с
    ``ConnectionError``/``EOFError``/``OSError``/``TimeoutError`` PyMax
    делает reconnect сам (через свой внутренний ``while True`` и ``if not
    extra_config.reconnect: raise``), но после чистого ``wait_closed()``
    он всё равно возвращается — и тут уже мы перехватываем.
    """
    while not stop_event.is_set():
        try:
            await client.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "client.start() crashed: %s; reconnecting in %.1fs",
                exc, client.extra_config.reconnect_delay,
            )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=client.extra_config.reconnect_delay,
                )
                # stop_event выставлен — выходим
                return
            except asyncio.TimeoutError:
                pass
        else:
            # Чистое завершение (PyMax закрыл long-poll соединение после sync,
            # ``else: return`` в pymax/base.py). Переинициализируем runtime
            # и снова запускаем start(). Сессия на диске жива, поэтому
            # повторный start() пройдёт через ``saved session loaded`` без
            # SMS/2FA.
            logger.info(
                "client.start() returned cleanly; resetting runtime and reconnecting in %.1fs",
                client.extra_config.reconnect_delay,
            )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=client.extra_config.reconnect_delay,
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                client._reset_runtime()
            except Exception as exc:
                logger.warning("client._reset_runtime failed: %s", exc)


def build_client(phone: str, cache_dir: str) -> Client:
    """Создаёт новый Client с нашими auth-провайдерами (pymax 2.2 API)."""
    flow = SmsAuthFlow(
        code_provider=QueueSmsCodeProvider(),
        password_provider=QueuePasswordProvider(),
    )
    # ``extra_config.reconnect=True`` — PyMax внутри ``start()`` сам делает
    # reconnect при ``ConnectionError``/``EOFError``/``OSError``/
    # ``TimeoutError``. Но после чистого ``wait_closed()`` (MAX сам закрыл
    # long-poll соединение после sync) PyMax возвращается через ``else:
    # return``. Это мы обрабатываем в ``_long_running_start`` wrapper'е.
    extra = ExtraConfig(
        reconnect=True,
        reconnect_delay=2.0,
    )
    client = Client(
        phone=phone,
        session_name="bridge",
        work_dir=cache_dir,
        auth_flow=flow,
        extra_config=extra,
    )
    register_bridge(client)
    return client