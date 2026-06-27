"""Пакет supervisor'а max-процесса.

Структура:

* ``_backoff``      — backoff-параметры (``NORMAL_POLL_SECONDS``,
                      ``RATE_LIMIT_BACKOFF``, ``SMS_RESEND_COOLDOWN`` и т.д.),
                      распознавание ошибок (``is_rate_limit_error``,
                      ``is_auth_error``), хелпер ``sleep_with_stop``.
* ``client_runtime`` — ``build_client`` (создаёт Client с нашими auth-flow)
                      и ``_long_running_start`` (обёртка вокруг ``client.start()``,
                      держит Client живым между чистыми возвратами).
* ``cache``         — ``wipe_cache`` (стирает PyMax session),
                      ``find_session_files`` (ищет кандидатов),
                      ``watch_session_file`` (фоновая задача).
* ``twofa_drain``   — ``drain_2fa_codes_loop`` (будит asyncio.Event
                      провайдеров, когда бот кладёт код через ``/code``).
* ``read_receipts`` — ``read_receipts_loop`` (помечает прочитанные
                      сообщения в MAX через ``client.read_message``).

``max/app/supervisor.py`` — главный цикл ``run()``, импортируется из
``max/run.py``. Он же собирает state-machine обработки команд и lifecycle.
"""