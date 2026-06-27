# max-bridge-pymax

Личный «мост» между мессенджером **MAX** и **Telegram-ботом** на базе
[PyMax](https://github.com/MaxApiTeam/PyMax) **2.2.0**
(вендорится в [`vendor/pymax/`](./vendor/pymax/)). Без Playwright,
без Chromium, без VNC.

Три процесса (`api` + `bot` + `max`) в одном Docker-контейнере под
`supervisord`, общаются через общую SQLite-БД.

Подробная документация — в [`docs/`](./docs/index.md). Ниже — только
обзор и быстрый старт.

## Что умеет

- Базовые команды: `/start`, `/chats`, `/status`, `/help`.
- Реплай и авторизация: `/reply`, `/code`, `/reauth_sms`.
- Супергруппа: `/setgroup`, `/supergroup`, `/prune_topics` (топики =
  MAX-чаты).
- Chat-ops (admin): `/resolve`, `/join`, `/invite`, `/search_user`,
  `/pending`, `/approve`, `/decline`, `/chatops`.

Полный список — [`docs/features.md`](./docs/features.md).

## Требования

- Docker Engine ≥ 24, docker compose v2.
- 512 МБ RAM хватит.
- TCP-порт `API_PORT` (по умолчанию 8000), если пробрасываете наружу.

## Быстрый старт

```bash
cd max-bridge-pymax

cp .env.example .env
$EDITOR .env
#   TELEGRAM_BOT_TOKEN=...          # @BotFather
#   ALLOWED_TG_USER_IDS=123456789   # @userinfobot
#   MAX_PHONE=+79xxxxxxxxx
#   BRIDGE_API_KEY=...              # make gen-key

make build
make up
make logs
```

MAX пришлёт SMS, бот пришлёт вам в Telegram:

> 🔐 MAX запрашивает код. Откройте SMS и ответьте: `/code 12345`.

После ввода кода:

> ✅ MAX: вход выполнен успешно.

## Документация

| Раздел | Файл |
| --- | --- |
| Архитектура | [`docs/architecture.md`](./docs/architecture.md) |
| Команды бота и ограничения | [`docs/features.md`](./docs/features.md) |
| Структура каталогов | [`docs/structure.md`](./docs/structure.md) |
| Зависимости | [`docs/dependencies.md`](./docs/dependencies.md) |
| Очереди в SQLite | [`docs/queues.md`](./docs/queues.md) |
| Авторизация (SMS/2FA) | [`docs/auth.md`](./docs/auth.md) |
| `make` и API | [`docs/commands.md`](./docs/commands.md) |
| Деплой (локально + Railway) | [`docs/deployment.md`](./docs/deployment.md) |
| Troubleshooting | [`docs/troubleshooting.md`](./docs/troubleshooting.md) |
| Оглавление | [`docs/index.md`](./docs/index.md) |

## Лицензия

Только для личного использования. PyMax использует неофициальный API
MAX — используйте на свой страх и риск.