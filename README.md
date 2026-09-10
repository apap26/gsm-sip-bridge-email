# gsm-sip-bridge-email

Отдельный sidecar-сервис, который читает входящие SMS из SQLite-базы
[`gsm-sip-bridge`](https://github.com/selvakn/gsm-sip-bridge) и отправляет их
по SMTP. Сервис не изменяет исходную базу и не использует ее
`forwarding_status`.

## Как это работает

- Compose монтирует весь `bridge-data` в `/bridge-data` только для чтения. Это
  сохраняет доступ к `store.db-wal` и `store.db-shm`.
- SQLite открывается с `mode=ro` и `PRAGMA query_only=ON`.
- Успешно отправленные `sms.id` записываются в отдельный `/data/state.db`.
- SMTP-ошибка не записывает ID: сообщение повторяется при следующем опросе.
- Письмо имеет тему `SMS from <number>` и UTF-8 тело с отправителем, номером
  принимающего модуля (`sms.module_id`), временем и текстом.
- Логи выводятся в JSON. Пароль SMTP не логируется.

Доставка имеет семантику at-least-once. Редкий сбой процесса между успешным
ответом SMTP и записью в `state.db` может привести к повторному письму;
стабильный `Message-ID` позволяет принимающей стороне его дедуплицировать.

## Настройка

Требуются соседние каталоги `~/gsm-sip-bridge` и
`~/gsm-sip-bridge-email`. Compose-интеграция находится в
`~/gsm-sip-bridge/docker/docker-compose.yml`.

Создайте конфигурацию с секретами, которая игнорируется Git:

```bash
cd ~/gsm-sip-bridge-email
cp .env.example .env
chmod 600 .env
```

Заполните `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
`SMTP_FROM` и `SMTP_TO`. Несколько получателей в `SMTP_TO` разделяются
запятыми. Для SMTPS обычно задают `SMTP_PORT=465`, `SMTP_SSL=true` и
`SMTP_STARTTLS=false`. Для STARTTLS обычно используются порт `587`,
`SMTP_STARTTLS=true` и `SMTP_SSL=false`.

До запуска выполните preflight. Он проверяет настройки, read-only доступ к
рабочей SMS-базе, возможность записи в отдельный state volume и соединение с
SMTP (включая TLS и авторизацию), но не отправляет письмо:

```bash
cd ~/gsm-sip-bridge/docker
docker compose build gsm-sip-bridge-email
docker compose run --rm --no-deps \
  --entrypoint python gsm-sip-bridge-email /app/preflight.py
```

Успешный результат содержит `"status": "ok"`. При ошибке исправьте `.env` и
повторите preflight; не запускайте постоянный контейнер до успешной проверки.

## Запуск

Обычный стек:

```bash
cd ~/gsm-sip-bridge/docker
docker compose up -d --build gsm-sip-bridge-email
docker compose logs -f gsm-sip-bridge-email
```

Стек с cellular internet:

```bash
docker compose -f docker-compose.yml \
  -f docker-compose.cellular-internet.yml up -d --build
```

Сервис не публикует порты. Его отказ не влияет на запуск и работу
`gsm-sip-bridge`.

## Проверка

```bash
python3 -m unittest discover -s tests -v
docker build -t gsm-sip-bridge-email:test .

cd ~/gsm-sip-bridge/docker
docker compose config --quiet
docker compose -f docker-compose.yml \
  -f docker-compose.cellular-internet.yml config --quiet
```

Healthcheck проверяет, что цикл опроса обновлял heartbeat не позднее чем три
интервала назад (минимальный допуск 60 секунд). Недоступность SMTP отражается
в логах, но не делает контейнер unhealthy: сервис продолжает повторные
попытки.

## Резервное копирование

Состояние хранится в named volume `email-forwarder-data`. Для полного сброса
истории остановите сервис и удалите этот volume. После сброса все SMS из
исходной базы будут отправлены повторно.

## Переменные

| Переменная | Обязательна | По умолчанию | Назначение |
| --- | --- | --- | --- |
| `SMTP_HOST` | да | - | SMTP-сервер |
| `SMTP_PORT` | нет | `587` | SMTP-порт |
| `SMTP_USERNAME` | нет | - | Имя пользователя; задается вместе с паролем |
| `SMTP_PASSWORD` | нет | - | Пароль; задается вместе с пользователем |
| `SMTP_FROM` | да | - | Адрес отправителя |
| `SMTP_TO` | да | - | Получатели через запятую |
| `SMTP_STARTTLS` | нет | `true` | Включить STARTTLS |
| `SMTP_SSL` | нет | `false` | Использовать SMTPS с начала соединения |
| `POLL_INTERVAL_SECONDS` | нет | `30` | Интервал опроса |
| `SMTP_TIMEOUT_SECONDS` | нет | `30` | Таймаут SMTP |
| `DB_BUSY_TIMEOUT_SECONDS` | нет | `5` | Ожидание блокировки SQLite |
| `BATCH_SIZE` | нет | `100` | Размер порции SMS |
| `LOG_LEVEL` | нет | `INFO` | Уровень логирования |

Пути `SMS_DB_PATH`, `STATE_DB_PATH` и `HEALTH_FILE` уже безопасно заданы в
Compose и обычно не требуют изменения.
