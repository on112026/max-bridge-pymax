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
logs-api: ## Логи только api-процесса (supervisor:bridge:api)
	$(COMPOSE) exec $(SERVICE) tail -F /dev/stdout 2>/dev/null | head

.PHONY: shell
shell: ## Войти в bash контейнера
	$(COMPOSE) exec $(SERVICE) bash

.PHONY: supervisorctl
supervisorctl: ## supervisorctl внутри контейнера
	$(COMPOSE) exec $(SERVICE) supervisorctl -c /etc/supervisor/supervisord.conf

.PHONY: restart-api
restart-api: ## Перезапустить api внутри supervisord
	$(COMPOSE) exec $(SERVICE) supervisorctl -c /etc/supervisor/supervisord.conf restart api

.PHONY: restart-bot
restart-bot: ## Перезапустить bot внутри supervisord
	$(COMPOSE) exec $(SERVICE) supervisorctl -c /etc/supervisor/supervisord.conf restart bot

.PHONY: restart-max
restart-max: ## Перезапустить max внутри supervisord
	$(COMPOSE) exec $(SERVICE) supervisorctl -c /etc/supervisor/supervisord.conf restart max

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
	@echo "Кэш PyMax очищен. Перезапустите: make restart-max"

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