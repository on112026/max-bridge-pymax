# Документация `max-bridge-pymax`

Расширенная документация по разделам. Для быстрого старта см.
[`../README.md`](../README.md).

## Карта документации

| Файл | Что внутри |
| --- | --- |
| [`architecture.md`](./architecture.md) | схема процессов, общая SQLite-БД, роли `api`/`bot`/`max` |
| [`features.md`](./features.md) | все команды бота, что шлём в Telegram / в MAX, ограничения PyMax |
| [`structure.md`](./structure.md) | карта файлов и модулей проекта |
| [`dependencies.md`](./dependencies.md) | таблица «пакет → зачем → где объявлен», что удалено и почему |
| [`queues.md`](./queues.md) | таблицы SQLite, направления потоков, цикл-воркеры |
| [`auth.md`](./auth.md) | flow SMS/2FA, persistent session, `/reauth_sms` |
| [`commands.md`](./commands.md) | все `make`-команды и HTTP API-эндпойнты |
| [`deployment.md`](./deployment.md) | локальный docker-compose + Railway |
| [`troubleshooting.md`](./troubleshooting.md) | типовые проблемы и что делать |

## Если вы здесь впервые

1. Прочитайте [`../README.md`](../README.md) — там быстрый старт.
2. Если что-то непонятно про архитектуру — [`architecture.md`](./architecture.md).
3. Если ищете команду бота — [`features.md`](./features.md).
4. Если что-то сломалось — [`troubleshooting.md`](./troubleshooting.md).