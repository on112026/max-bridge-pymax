"""Цикл chat-операций MAX-процесса + хранитель ссылки на ``pymax.Client``.

Зачем нужен этот модуль
-----------------------

В ``pymax`` 2.2.0 есть публичные методы для админских операций над чатами:

* ``client.join_group(link)`` / ``client.join_channel(link)`` — вступить;
* ``client.resolve_group_by_link(link)`` — превью чата по ссылке;
* ``client.invite_users_to_group(chat_id, user_ids, show_history)`` —
  пригласить пользователей;
* ``client.invite_users_to_channel(chat_id, user_ids, show_history)`` —
  то же для канала;
* ``client.search_by_phone(phone)`` — найти user_id по номеру телефона
  (единственный публичный метод поиска пользователя);
* ``client.get_join_requests(chat_id)`` / ``confirm_join_request(s)`` /
  ``decline_join_request(s)`` — заявки на вступление.

Их надо дёргать из MAX-процесса (там, где живёт ``Client``), а запросы
приходят из TG-бота → API → ... → MAX-процесс. Архитектура моста —

* API принимает HTTP-запрос от бота, кладёт задачу в SQLite-таблицу
  ``chat_ops_queue`` (``shared/db/chat_ops_queue.py``).
* MAX-процесс в этой корутине периодически забирает pending-задачи
  и выполняет их через ``pymax.Client``.

Это полностью повторяет паттерн ``sender_loop`` (``max/app/sender.py``) и
``read_receipts_loop`` (``max/app/supervisor/read_receipts.py``) — мы
держим единый стиль: API пишет в БД, MAX читает из БД.

Структура файла:

* :func:`set_client` / :func:`clear_client` / :func:`get_client` /
  :func:`is_ready` — публикация ссылки на живой ``Client`` из supervisor'а.
* :func:`_do_op` — диспетчер: вызывает нужный метод ``Client`` по ``op``.
* :func:`chat_ops_loop` — основной polling-цикл.

Использование в supervisor (``max/app/supervisor/__init__.py``):

.. code-block:: python

    from app import chat_ops

    client = build_client(phone, cache_dir)
    chat_ops.set_client(client)
    chat_ops_task = asyncio.create_task(
        chat_ops.chat_ops_loop(stop_event), name="chat-ops",
    )
    # ... в блоке очистки:
    chat_ops.clear_client()
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Интервал между опросами очереди (сек). Задачи редкие, можно подольше.
POLL_INTERVAL = 2.0

# Таймаут ожидания готового Client (после старта supervisor'а).
# Если Client ещё не поднят (``auth_required``) — задачи просто копятся в БД.
READY_CHECK_INTERVAL = 2.0


# ---------------------------------------------------------------------------
# Хранитель ссылки на живой ``Client``
# ---------------------------------------------------------------------------

_client: Optional["object"] = None
_lock = threading.Lock()


def set_client(client) -> None:
    """Зарегистрировать живой ``Client``.

    Вызывается из supervisor сразу после ``build_client(...)``.
    Повторный вызов без промежуточного :func:`clear_client` логируется
    как warning, но всё равно перезаписывает ссылку.
    """
    global _client
    with _lock:
        if _client is not None and client is not None and _client is not client:
            logger.warning(
                "chat_ops.set_client: replacing existing live Client reference "
                "(old id=%s, new id=%s)",
                id(_client), id(client),
            )
        _client = client
        if client is None:
            logger.debug("chat_ops.set_client(None) — client cleared")
        else:
            logger.debug("chat_ops.set_client: live Client registered (id=%s)", id(client))


def clear_client() -> None:
    """Сбросить ссылку на клиент. Вызывается при штатном завершении/перезапуске."""
    set_client(None)


def get_client():
    """Вернуть текущий живой ``Client`` или ``None``."""
    with _lock:
        return _client


def is_ready() -> bool:
    """``True``, если клиент жив и можно вызывать его методы."""
    return get_client() is not None


# ---------------------------------------------------------------------------
# Сериализация результатов (pymax-объекты → JSON-safe dict)
# ---------------------------------------------------------------------------


def _chat_to_dict(chat: Any) -> dict:
    """Превратить ``pymax.Chat`` (или похожий объект) в JSON-safe ``dict``.

    Pymax возвращает ``Chat`` с атрибутами (``id``, ``title``, ``type`` и т.д.);
    ``model_dump()`` или ``__dict__`` — самый надёжный способ без жёсткой
    привязки к конкретной версии pymax (мы не правим вендор).
    """
    if chat is None:
        return {}
    # Pydantic v2: ``model_dump``.
    if hasattr(chat, "model_dump"):
        try:
            return chat.model_dump()
        except Exception:
            pass
    # Pydantic v1: ``dict``.
    if hasattr(chat, "dict") and callable(chat.dict):
        try:
            return chat.dict()
        except Exception:
            pass
    # Фолбэк: ``__dict__``.
    raw = getattr(chat, "__dict__", {}) or {}
    return {k: _jsonify(v) for k, v in raw.items() if not k.startswith("_")}


def _user_to_dict(user: Any) -> dict:
    """Превратить ``pymax.User`` (или похожий объект) в JSON-safe ``dict``."""
    if user is None:
        return {}
    if hasattr(user, "model_dump"):
        try:
            return user.model_dump()
        except Exception:
            pass
    if hasattr(user, "dict") and callable(user.dict):
        try:
            return user.dict()
        except Exception:
            pass
    raw = getattr(user, "__dict__", {}) or {}
    return {k: _jsonify(v) for k, v in raw.items() if not k.startswith("_")}


def _jsonify(value: Any) -> Any:
    """Рекурсивно превратить значение в JSON-safe (str/int/float/bool/list/dict/None)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(v) for v in value]
    # Pydantic-модели (Chat/User/Message) — пробуем сериализовать.
    if hasattr(value, "model_dump"):
        try:
            return _jsonify(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "dict") and callable(value.dict):
        try:
            return _jsonify(value.dict())
        except Exception:
            pass
    # Последний фолбэк — строковое представление.
    try:
        return str(value)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Диспетчер операций
# ---------------------------------------------------------------------------


async def _do_op(op: str, payload: dict) -> Any:
    """Выполнить одну операцию через ``pymax.Client``.

    Возвращает JSON-сериализуемый результат или поднимает исключение
    (его поймает ``chat_ops_loop`` и пометит задачу ``failed``).
    """
    client = get_client()
    if client is None:
        raise RuntimeError("pymax Client ещё не готов (auth_required / session_attached)")

    if op == "join":
        link = (payload.get("link") or "").strip()
        if not link:
            raise ValueError("payload.link is empty")
        # Начиная с pymax 2.x, MAX изменил ссылку на ``https://max.ru/<token>``.
        # ``ChatLinkPrefix.JOIN = "join/"`` всё ещё работает как префикс
        # для ``join_group``/``join_channel`` (см. ``vendor/pymax/api/chats/enums.py``).
        # Сначала пробуем ``resolve_group_by_link`` — он может вернуть
        # информацию о чате; затем дёргаем ``join_group`` (если группа) или
        # ``join_channel`` (если канал). Pymax сам разбирается по ссылке
        # через префикс ``join/``.
        result = await client.join_group(link)
        # Многие версии pymax возвращают ``Chat`` после успешного join.
        return _chat_to_dict(result)

    if op == "resolve":
        link = (payload.get("link") or "").strip()
        if not link:
            raise ValueError("payload.link is empty")
        # ``resolve_group_by_link`` НЕ вступает в чат — только возвращает инфо.
        chat = await client.resolve_group_by_link(link)
        return _chat_to_dict(chat)

    if op == "invite":
        chat_id_raw = payload.get("chat_id")
        user_ids = payload.get("user_ids") or []
        show_history = bool(payload.get("show_history", True))
        if chat_id_raw is None:
            raise ValueError("payload.chat_id is empty")
        try:
            chat_id = int(chat_id_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"payload.chat_id must be int: {chat_id_raw!r}") from exc
        if not user_ids:
            raise ValueError("payload.user_ids is empty")
        # Нормализуем user_ids в list[int].
        user_ids_int = [int(x) for x in user_ids]
        # Пробуем оба варианта: сначала ``invite_users_to_group`` (он
        # универсальный в pymax 2.x и работает и для каналов тоже). Если
        # pymax выбросит AttributeError — фолбэк на ``invite_users_to_channel``.
        try:
            result = await client.invite_users_to_group(
                chat_id=chat_id, user_ids=user_ids_int, show_history=show_history,
            )
        except AttributeError:
            result = await client.invite_users_to_channel(
                chat_id=chat_id, user_ids=user_ids_int, show_history=show_history,
            )
        # Pymax возвращает ``bool``/``None``/``list[str]`` (список ошибок по юзерам).
        return _jsonify(result)

    if op == "list_join_requests":
        chat_id_raw = payload.get("chat_id")
        if chat_id_raw is None:
            raise ValueError("payload.chat_id is empty")
        chat_id = int(chat_id_raw)
        requests = await client.get_join_requests(chat_id)
        # ``requests`` может быть списком объектов JoinRequest.
        if requests is None:
            return []
        return _jsonify(requests)

    if op == "confirm_join_request":
        chat_id_raw = payload.get("chat_id")
        user_ids = payload.get("user_ids") or []
        if chat_id_raw is None:
            raise ValueError("payload.chat_id is empty")
        if not user_ids:
            raise ValueError("payload.user_ids is empty")
        chat_id = int(chat_id_raw)
        user_ids_int = [int(x) for x in user_ids]
        # ``confirm_join_requests`` (множественное число) — поддерживается
        # в свежих pymax; одиночный ``confirm_join_request`` — для совместимости.
        try:
            result = await client.confirm_join_requests(
                chat_id=chat_id, user_ids=user_ids_int,
            )
        except AttributeError:
            # Фолбэк: дёргаем по одному.
            results = []
            for uid in user_ids_int:
                ok = await client.confirm_join_request(chat_id=chat_id, user_id=uid)
                results.append({"user_id": uid, "ok": bool(ok)})
            result = results
        return _jsonify(result)

    if op == "decline_join_request":
        chat_id_raw = payload.get("chat_id")
        user_ids = payload.get("user_ids") or []
        if chat_id_raw is None:
            raise ValueError("payload.chat_id is empty")
        if not user_ids:
            raise ValueError("payload.user_ids is empty")
        chat_id = int(chat_id_raw)
        user_ids_int = [int(x) for x in user_ids]
        try:
            result = await client.decline_join_requests(
                chat_id=chat_id, user_ids=user_ids_int,
            )
        except AttributeError:
            results = []
            for uid in user_ids_int:
                ok = await client.decline_join_request(chat_id=chat_id, user_id=uid)
                results.append({"user_id": uid, "ok": bool(ok)})
            result = results
        return _jsonify(result)

    if op == "search_user":
        phone = (payload.get("phone") or "").strip()
        if not phone:
            raise ValueError("payload.phone is empty")
        # ``search_by_phone`` возвращает ``User | None``.
        user = await client.search_by_phone(phone)
        return _user_to_dict(user)

    raise ValueError(f"unknown chat_ops op: {op!r}")


# ---------------------------------------------------------------------------
# Polling-цикл
# ---------------------------------------------------------------------------


async def _claim_next() -> Optional[Any]:
    """Забрать следующую задачу из API через HTTP (как ``sender_loop``).

    Ходим в API через ``/chat_ops/next`` — так же, как
    ``max/app/sender.py::_claim_next`` ходит в ``/send/next``. Это позволяет
    API быть «правдой» по статусам и не дублировать логику claim'а.
    """
    import os
    import httpx

    api_base = os.getenv("API_BASE_URL", "http://localhost:8000")
    api_key = os.getenv("BRIDGE_API_KEY", "")
    try:
        async with httpx.AsyncClient(base_url=api_base, timeout=10.0) as c:
            r = await c.get("/chat_ops/next", headers={"X-Api-Key": api_key})
            if r.status_code == 200 and r.content:
                return r.json()
    except Exception as exc:
        logger.warning("chat_ops._claim_next failed: %s", exc)
    return None


async def _finish(item_id: int, ok: bool, error: Optional[str] = None,
                  result: Optional[Any] = None) -> None:
    """Сообщить API о завершении задачи (``POST /chat_ops/{id}/finish``)."""
    import os
    import httpx

    api_base = os.getenv("API_BASE_URL", "http://localhost:8000")
    api_key = os.getenv("BRIDGE_API_KEY", "")
    body = {"ok": ok, "error": error}
    if result is not None:
        # result уже должен быть JSON-сериализуемым.
        body["result"] = result
    try:
        async with httpx.AsyncClient(base_url=api_base, timeout=10.0) as c:
            r = await c.post(
                f"/chat_ops/{item_id}/finish",
                json=body,
                headers={"X-Api-Key": api_key},
            )
            if r.status_code >= 400:
                logger.warning(
                    "chat_ops._finish id=%s failed: %s %s",
                    item_id, r.status_code, r.text[:200],
                )
    except Exception as exc:
        logger.warning("chat_ops._finish id=%s exception: %s", item_id, exc)


async def _wait_or_stop(stop_event: asyncio.Event, timeout: float) -> bool:
    """Ждать ``stop_event`` или таймаут. Возвращает ``True``, если пришёл stop."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def chat_ops_loop(stop_event: asyncio.Event) -> None:
    """Основной polling-цикл: забирает задачи из API и выполняет их.

    Если Client ещё не готов (``auth_required``) — просто ждём, не дёргая API.
    Иначе — ``GET /chat_ops/next`` → выполнение через ``_do_op`` →
    ``POST /chat_ops/{id}/finish``.
    """
    logger.info("chat_ops_loop started (poll=%.1fs)", POLL_INTERVAL)
    while not stop_event.is_set():
        try:
            if not is_ready():
                # Client ещё не поднят — задачи копятся, мы их не теряем.
                await _wait_or_stop(stop_event, READY_CHECK_INTERVAL)
                continue

            item = await _claim_next()
            if item is None:
                await _wait_or_stop(stop_event, POLL_INTERVAL)
                continue

            item_id = item.get("id")
            op = (item.get("op") or "").strip()
            payload = item.get("payload") or {}
            if not item_id or not op:
                logger.warning("chat_ops_loop: malformed item=%r, skipping", item)
                await _wait_or_stop(stop_event, POLL_INTERVAL)
                continue

            logger.info("chat_ops_loop: claimed id=%s op=%s payload=%s", item_id, op, payload)
            try:
                result = await _do_op(op, payload)
                logger.info("chat_ops_loop: id=%s op=%s OK", item_id, op)
                await _finish(item_id, ok=True, error=None, result=result)
            except Exception as exc:
                logger.warning(
                    "chat_ops_loop: id=%s op=%s FAILED: %s", item_id, op, exc,
                )
                await _finish(item_id, ok=False, error=str(exc), result=None)

        except Exception as exc:
            logger.warning("chat_ops_loop tick error: %s", exc)

        await _wait_or_stop(stop_event, POLL_INTERVAL)