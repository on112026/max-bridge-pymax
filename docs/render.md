# Деплой на Render.com + Cloudflare D1

Данные (SQLite-БД моста + сессия PyMax) хранятся в **Cloudflare D1** —
бесплатно, без карты, только email-аккаунт Cloudflare.

При рестарте Render оба файла автоматически восстанавливаются из D1,
SMS-авторизация не требуется повторно.

---

## Шаг 1 — Создать D1 базу данных в Cloudflare

Нужен [Cloudflare-аккаунт](https://dash.cloudflare.com/sign-up) (email, без карты).

### Вариант A — через wrangler CLI (рекомендуется, npx уже есть)

```bash
# Войти в Cloudflare
npx wrangler login
# Откроется браузер → подтвердить

# Создать базу данных
npx wrangler d1 create max-bridge-db
```

Вывод будет примерно такой:
```
✅ Successfully created DB 'max-bridge-db'
database_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

Скопируйте `database_id` — это `CF_D1_DATABASE_ID`.

### Вариант B — через браузер

1. dash.cloudflare.com → **Workers & Pages** → **D1**
2. **Create database** → имя `max-bridge-db` → Create
3. Скопируйте **Database ID** со страницы базы

---

## Шаг 2 — Получить Account ID и API Token

**Account ID:**
- dash.cloudflare.com → любая страница → правый нижний угол → «Account ID»
- Или: Workers & Pages → Overview → правый sidebar

**API Token:**
1. dash.cloudflare.com → My Profile → **API Tokens** → **Create Token**
2. Шаблон: **Edit Cloudflare Workers** — или Custom Token:
   - Permissions: `Account` → `D1` → `Edit`
   - Account Resources: Include → ваш аккаунт
3. Continue to summary → Create Token
4. Скопируйте токен (показывается один раз)

---

## Шаг 3 — Залить локальные файлы в D1

У вас уже есть файлы с Railway. Задайте переменные и запустите:

```bash
# Задать переменные (или добавить в .env)
export CF_ACCOUNT_ID="ваш_account_id"
export CF_API_TOKEN="ваш_api_token"
export CF_D1_DATABASE_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

# Пути к вашим локальным файлам (скопированным с Railway)
export DB_PATH="/путь/к/папке/bridge.db"          # основная БД моста
export CACHE_DIR="/путь/к/папке"                   # папка, где лежит сессия bridge.db

# Создать таблицу в D1 (один раз)
python render_d1.py init

# Залить оба файла
python render_d1.py push-all
```

Ожидаемый вывод:
```
✅ push-db: /путь/к/bridge.db
✅ push-session: /путь/к/cache/bridge.db
```

Проверка:
```bash
python render_d1.py status
# D1 meta_blobs:
#   db            42 KB  обновлено 2026-08-02T10:00:00Z
#   session       68 KB  обновлено 2026-08-02T10:00:00Z
```

> **Важно:** если оба файла называются `bridge.db` но лежат в разных папках —
> это нормально. `DB_PATH` — полный путь к основной БД, `CACHE_DIR` — папка
> где PyMax хранит сессию (там тоже `bridge.db`). Они кладутся в D1 под
> разными ключами: `db` и `session`.

---

## Шаг 4 — Деплой на Render

1. **Залить репо на GitHub** (если ещё не сделано)

2. **render.com → New → Web Service → Connect a repository**

3. Render обнаружит `Dockerfile`. Настройки:
   - **Start Command**: `sh /app/render_start.sh`
   - **Health Check Path**: `/health`

4. **Environment → Add environment variables:**

   | Переменная | Значение |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | токен от @BotFather |
   | `ALLOWED_TG_USER_IDS` | ваш Telegram user_id |
   | `MAX_PHONE` | +79000000000 |
   | `BRIDGE_API_KEY` | длинный ключ (`make gen-key`) |
   | `CF_ACCOUNT_ID` | из шага 2 |
   | `CF_API_TOKEN` | из шага 2 |
   | `CF_D1_DATABASE_ID` | из шага 1 |

5. **Deploy** → в логах должно появиться:
   ```
   [render_start] ✅ bridge.db restored (43008 bytes)
   [render_start] ✅ PyMax session restored (69632 bytes) — SMS not required
   bridge starting: api + bot + max in one process
   ```

6. После деплоя скопируйте URL (вида `https://max-bridge.onrender.com`)
   и добавьте переменную `RENDER_SERVICE_URL=https://max-bridge.onrender.com`
   → это включит keep-alive (сервис не будет засыпать).

---

## Как это работает при рестарте

```
Render рестартует контейнер
        ↓
render_start.sh
        ↓
python render_d1.py pull-all
  ├── D1 → /data/bridge.db        (основная БД, состояние очередей)
  └── D1 → /data/cache/bridge.db  (сессия PyMax, авторизация MAX)
        ↓
python run_all.py
  ├── api  (FastAPI) — читает /data/bridge.db ✓
  ├── bot  (aiogram) — состояние восстановлено ✓
  └── max  (PyMax)  — сессия на диске, SMS не нужен ✓
        ↓
Каждые 5 минут: push-all → D1 (актуальное состояние)
```

---

## Keep-alive: не давать Render засыпать

Render Free усыпляет сервис через **15 минут** без HTTP-запросов.
Telegram-бот — polling-based, он сам не делает входящих HTTP к Render.

**Встроенный пинг** (если задан `RENDER_SERVICE_URL`):
`render_start.sh` пингует `/health` каждые 10 минут изнутри.

**Надёжнее — внешний пинг:**
- [cron-job.org](https://cron-job.org) → Create cronjob →
  URL: `https://max-bridge.onrender.com/health`, интервал: 10 мин, бесплатно

---

## Обновить данные в D1 вручную

```bash
# Обновить только сессию (например после /reauth_sms)
export CF_ACCOUNT_ID=... CF_API_TOKEN=... CF_D1_DATABASE_ID=...
export CACHE_DIR=/путь/к/cache
python render_d1.py push-session

# Обновить всё
python render_d1.py push-all

# Проверить что лежит в D1
python render_d1.py status
```

Или если проект запущен локально в Docker:
```bash
make d1-push-from-docker
```

---

## Лимиты D1 Free (для понимания масштаба)

| Лимит | Значение |
|---|---|
| Хранилище | 5 GB |
| Чтений в день | 5 млн строк |
| Записей в день | 100 тыс строк |

Наш мост пишет в D1 раз в 5 минут (2 строки UPDATE).
За день: 2 × 12 × 24 = **576 строк записей**. До лимита как до луны.

---

## Troubleshooting

**`[render_start] ⚠️ PyMax session not in D1`** → SMS будет запрошен.
Залейте сессию: `python render_d1.py push-session`

**`D1 API HTTP 403`** → неверный токен или нет прав D1:Edit.
Пересоздайте API Token с правами Account → D1 → Edit.

**`D1 API HTTP 404`** → неверный `CF_D1_DATABASE_ID` или `CF_ACCOUNT_ID`.
Проверьте: `npx wrangler d1 list`

**Данные не восстанавливаются** → проверьте логи:
```
[render_start] restoring from Cloudflare D1...
```
Если этой строки нет — не заданы CF_* переменные на Render.
