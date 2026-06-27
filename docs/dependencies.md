# Зависимости

Проект собирается в один Docker-образ: в `Dockerfile` последовательно
ставятся все четыре `requirements.txt`. PyMax вендорится в
`vendor/pymax`, его runtime-зависимости изолированы в
`max/requirements.txt`.

## Общая таблица

| Пакет | Зачем | requirements.txt |
| --- | --- | --- |
| `fastapi`, `uvicorn[standard]` | HTTP API | корень |
| `SQLAlchemy` | ORM + SQLite (общая БД моста) | корень, api, bot, max |
| `pydantic` | схемы API, модели PyMax | корень, api, bot, max |
| `httpx` | HTTP-клиент: бот↔API, max↔API, chat_ops_loop | корень, bot, max |
| `python-multipart` | `UploadFile` в `/sessions/upload` | корень |
| `aiogram` | Telegram-бот | bot |
| `aiohttp` | HTTP-клиент PyMax | max |
| `aiofiles` | асинхронное чтение медиа PyMax | max |
| `aiosqlite` | SQLite-кэш PyMax | max |
| `msgpack` | бинарная сериализация протокола MAX | max |
| `websockets` | транспорт PyMax | max |
| `python-socks[asyncio]` | SOCKS-прокси (опционально) | max |
| `qrcode` | QR-код при первичной авторизации | max |

## Что где объявлено

### `requirements.txt` (корень)

Общие runtime-зависимости для всех трёх процессов:

```text
fastapi==0.115.6
uvicorn[standard]==0.32.1
SQLAlchemy==2.0.36
pydantic>=2.4.1,<2.10
httpx==0.28.1
python-multipart==0.0.9
```

### `api/requirements.txt`

То же, что в корне, минус `python-multipart` (нужен только api, но он
уже в корне).

### `bot/requirements.txt`

```text
aiogram==3.15.0
httpx==0.28.1
SQLAlchemy==2.0.36
pydantic>=2.4.1,<2.10
```

### `max/requirements.txt`

Полный pymax-runtime + дубли общих для самодостаточности:

```text
aiofiles>=23.2.1,<24.2
aiohttp==3.10.10
aiosqlite>=0.22.1
msgpack>=1.1.2
pydantic>=2.4.1,<2.10
python-socks[asyncio]>=2.8.1
qrcode>=8.2
websockets>=16.0

httpx==0.28.1
SQLAlchemy==2.0.36
```

## Что НЕ используется (и удалено)

При аудите кода подтверждено:

- `python-dotenv` — 0 импортов, `load_dotenv` нигде не вызывается.
  Конфиг читается через `os.getenv` в `shared/config.py`.
- `aiohttp` в `api/` и `bot/` — pymax импортируется только в `max/`,
  в api и bot он не нужен.
- Дубли `SQLAlchemy` / `pydantic` / `httpx` в разных `requirements.txt`
  безвредны (pip дедуплицирует), но в `max/requirements.txt` они
  нужны для самодостаточности.

## Замечание про PyMax

> **PyMax — непубличный протокол, его API может сломаться при
> обновлении MAX.** Версия PyMax зафиксирована в `vendor/`, апгрейд —
> через замену вендора (см. `vendor/README.md`).

## Дальше

- Что и куда течёт по таблицам SQLite — [`queues.md`](./queues.md).
- SMS/2FA flow — [`auth.md`](./auth.md).