# Деплой

## Локально (docker-compose)

```bash
cd max-bridge-pymax

cp .env.example .env
$EDITOR .env
#   TELEGRAM_BOT_TOKEN=...
#   ALLOWED_TG_USER_IDS=123456789
#   MAX_PHONE=+79xxxxxxxxx
#   BRIDGE_API_KEY=...    # make gen-key

make build
make up
make logs
```

Внутри одного контейнера крутятся три процесса (`api`, `bot`, `max`)
под `supervisord`. На хосте — тома `/data` (БД + медиа) и `/app/cache`
(сессия PyMax).

### Полезные команды

```bash
make status          # GET /status API
make chats           # GET /chats?limit=20
make events          # GET /events?limit=20
make shell           # bash внутрь контейнера
make supervisorctl   # supervisorctl внутрь
make restart-api     # перезапустить api внутри
make restart-bot     # перезапустить bot внутри
make restart-max     # перезапустить max внутри
make wipe-cache      # стереть сессию PyMax (== reauth с нуля)
make wipe-data       # ⚠️ стереть /data (БД + медиа)
make nuke            # down + wipe-data
make gen-key         # сгенерировать BRIDGE_API_KEY
```

Подробнее — [`commands.md`](./commands.md).

## Railway

1. Залить репозиторий (или подключить текущий) в Railway.
2. **New Project → Deploy from GitHub → выбрать репо**.
3. Railway сам подхватит `Dockerfile`. Переменные окружения задать
   через **Variables** (`TELEGRAM_BOT_TOKEN`, `ALLOWED_TG_USER_IDS`,
   `MAX_PHONE`, `BRIDGE_API_KEY`, при необходимости `API_PORT`).
4. В **Settings → Volumes** добавить два тома:
   - `/data` (для SQLite + скачанных медиа),
   - `/app/cache` (для сессии PyMax).
5. **Settings → Healthcheck Path**: `/health`.
6. Дождаться деплоя. Логи — во вкладке **Logs** (`supervisord` сразу
   видно).
7. SMS-флоу проходит так же, как локально: бот пришлёт вам код-реквест
   в Telegram.

> **Только один контейнер.** Railway-сервис — это один Dockerfile, у
> нас внутри supervisord с 3 процессами. Пробрасывать наружу порт
> `API_PORT` необязательно: бот ходит в API по `127.0.0.1` внутри
> одного контейнера. Порт нужен, только если хотите смотреть
> `/status` снаружи — тогда включите **Settings → Networking → Port**.

## Дальше

- Troubleshooting — [`troubleshooting.md`](./troubleshooting.md).