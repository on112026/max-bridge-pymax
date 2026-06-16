# Vendor: PyMax 2.2.0

Эта копия взята из официального репозитория
[`MaxApiTeam/PyMax`](https://github.com/MaxApiTeam/PyMax) (ветка `main`,
исходник в `src/pymax/`) и положена в `vendor/pymax/`.

PyMax использует **неофициальный внутренний API Max**. По заявлению автора
протокол может измениться без предупреждения. Поэтому:

- Версия PyMax зафиксирована в `vendor/VERSION` (сейчас `2.2.0`).
- При баге в библиотеке патчим прямо здесь, не ждём апстрима.
- Обновлять PyMax только осознанно: читаем CHANGELOG, проверяем наш
  `run_poc.py` / `max/app/`, и только после этого поднимаем версию.

## Зачем вендорить, а не ставить из PyPI

`maxapi-python` из PyPI — это та же самая кодовая база. Если апстрим
пропадёт, удалит релиз или встроит бэкдор, наш prod-контейнер окажется
без зависимости. Вендоринг даёт нулевой риск «вырвали PyPI» и позволяет
точечно патчить сетевой/авторизационный код под поведение MAX.

## Как обновить (когда реально понадобится)

```bash
cd max-bridge-pymax
rm -rf vendor/pymax
git clone --depth 1 --branch vX.Y.Z https://github.com/MaxApiTeam/PyMax /tmp/pymax
cp -r /tmp/pymax/src/pymax vendor/pymax
echo "X.Y.Z" > vendor/VERSION
# прогнать run_poc.py, убедиться что логин/сообщения работают
```

## Импорт в коде

```python
from pymax import Client, Message  # vendor/pymax/__init__.py
```

`PYTHONPATH=/app:/app/vendor` (см. Dockerfile и README) — этого достаточно.