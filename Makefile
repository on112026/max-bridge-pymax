# Makefile для max-bridge-pymax (этап 2).
# Все команды выполняются в корне проекта.

SHELL := /bin/bash

COMPOSE := docker compose
SERVICE := bridge

# По умолчанию: help
.DEFAULT_GOAL := help

.PHONY: help
help: ## Показать список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Сборка и запуск ----------------------------------------------------

.PHONY: build
build: ## Собрать образ
	$(COMPOSE) build

.PHONY: up
up: ## Поднять контейнер в фоне
	$(COMPOSE) up -d
	@echo "Готово. Логи: make logs"

.PHONY: down
down: ## Остановить контейнер
	$(COMPOSE) down

.PHONY: restart
restart: ## Перезапустить контейнер
	$(COMPOSE) restart

.PHONY: ps
ps: ## Статус контейнера
	$(COMPOSE) ps

# --- Логи / отладка -----------------------------------------------------

.PHONY: logs
logs: ## Трейс логов всего контейнера
	$(COMPOSE) logs -f --tail=200

.PHONY: logs-api
logs-api: ## Логи только api (фильтр по логгерам api.*)
	$(COMPOSE) logs -f --tail=500 | grep -E ' (api\.|run_all)' --color=never

.PHONY: logs-bot
logs-bot: ## Логи только bot (фильтр по логгерам app.*, это namespace бота)
	$(COMPOSE) logs -f --tail=500 | grep -E ' (app\.|aiogram)' --color=never

.PHONY: logs-max
logs-max: ## Логи только max (фильтр по логгерам maxcore.*/pymax)
	$(COMPOSE) logs -f --tail=500 | grep -E ' (maxcore\.|pymax)' --color=never

.PHONY: shell
shell: ## Войти в bash контейнера
	$(COMPOSE) exec $(SERVICE) bash

# --- Состояние MAX ------------------------------------------------------

.PHONY: status
status: ## Показать /status API (auth, last_error)
	@curl -sS -H "X-Api-Key: $${BRIDGE_API_KEY}" http://127.0.0.1:$${API_PORT:-8000}/status | python -m json.tool

.PHONY: chats
chats: ## Показать последние 20 чатов MAX из локальной БД
	@curl -sS -H "X-Api-Key: $${BRIDGE_API_KEY}" "http://127.0.0.1:$${API_PORT:-8000}/chats?limit=20" | python -m json.tool

.PHONY: events
events: ## Показать последние 20 событий (входящих) из API
	@curl -sS -H "X-Api-Key: $${BRIDGE_API_KEY}" "http://127.0.0.1:$${API_PORT:-8000}/events?limit=20" | python -m json.tool

# --- Сброс/обслуживание ------------------------------------------------

.PHONY: wipe-cache
wipe-cache: ## Удалить кэш PyMax (== «reauth с нуля»). НЕ стирает /data.
	$(COMPOSE) exec $(SERVICE) rm -rf /app/cache/*
	@echo "Кэш PyMax очищен. Перезапустите контейнер: make restart"

.PHONY: wipe-data
wipe-data: ## ⚠️ Полный сброс БД и медиа
	@echo "Это удалит /data/bridge.db и /data/media/* (необратимо)."
	@read -p "Продолжить? [y/N] " r && [[ $$r =~ ^[Yy]$$ ]]
	$(COMPOSE) down
	docker volume rm max-bridge-pymax_bridge_data || true
	@echo "Готово. Поднимите заново: make up"

.PHONY: nuke
nuke: down wipe-data ## down + wipe-data

# --- Утилиты -----------------------------------------------------------

.PHONY: env
env: ## Показать текущие .env (без секретов)
	@grep -v -E '(TOKEN|KEY|SECRET|PASSWORD)' .env 2>/dev/null | grep -v '^#' | grep -v '^$$' || true

.PHONY: gen-key
gen-key: ## Сгенерировать BRIDGE_API_KEY
	@openssl rand -hex 32

# --- Render.com / Cloudflare D1 helpers --------------------------------
# Перед использованием задайте в .env или окружении:
#   CF_ACCOUNT_ID, CF_API_TOKEN, CF_D1_DATABASE_ID
#   DB_PATH (по умолчанию /data/bridge.db)
#   CACHE_DIR (по умолчанию /data/cache)

.PHONY: d1-init
d1-init: ## Создать таблицу meta_blobs в D1 (один раз перед первым push)
	python render_d1.py init

.PHONY: d1-push-all
d1-push-all: ## Залить bridge.db + сессию PyMax в D1 (использовать перед деплоем на Render)
	python render_d1.py push-all

.PHONY: d1-push-db
d1-push-db: ## Залить только bridge.db в D1
	python render_d1.py push-db

.PHONY: d1-push-session
d1-push-session: ## Залить только сессию PyMax в D1
	python render_d1.py push-session

.PHONY: d1-pull-all
d1-pull-all: ## Скачать bridge.db + сессию из D1 локально (для проверки)
	python render_d1.py pull-all

.PHONY: d1-status
d1-status: ## Показать что лежит в D1
	python render_d1.py status

.PHONY: d1-push-from-docker
d1-push-from-docker: ## Залить файлы из работающего контейнера в D1
	@echo "Копируем файлы из контейнера..."
	@docker compose exec $(SERVICE) python /app/render_d1.py push-all

# --- Полный PoC-цикл (этап 1, не для прод-использования) --------------

.PHONY: poc-build
poc-build: ## Собрать PoC-контейнер (этап 1)
	docker build -f Dockerfile.poc -t max-bridge-pymax:poc .

.PHONY: poc-up
poc-up: ## Поднять PoC
	docker compose -f docker-compose.poc.yaml up --build

.PHONY: poc-down
poc-down: ## Остановить PoC
	docker compose -f docker-compose.poc.yaml down