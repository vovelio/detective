# Локальная проверка API

Нужны Python 3.13.3, Node 24.15.0/npm 11.12.1 для проверок frontend и PostgreSQL 18.
Команды выполняются из корня проекта. Публичный сервер не требуется.

## Зависимости

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
export PYTHONPATH="$PWD/backend"
```

PYTHONPATH также позволяет запускать исходники на macOS, если система пометила
служебный editable .pth как скрытый и Python его не подхватывает.

## Отдельный локальный PostgreSQL

Пример для установленного PostgreSQL на macOS (на Linux укажите свой каталог binaries).
Используйте новый каталог: initdb безопасно откажется работать с непустым кластером.
Эти команды не обращаются к существующим базам. Порт 55440 доступен только на loopback.
Trust применяется только к этому локальному учебному кластеру без настоящих данных.

```sh
export DETECTIVE_PG_BIN="/Library/PostgreSQL/18/bin"
mkdir -p .local
"$DETECTIVE_PG_BIN/initdb" -D "$PWD/.local/pg" -U detective_local -A trust --no-locale -E UTF8
"$DETECTIVE_PG_BIN/pg_ctl" -D "$PWD/.local/pg" -l "$PWD/.local/postgres.log" \
  -o "-h 127.0.0.1 -p 55440 -k $PWD/.local" start
"$DETECTIVE_PG_BIN/createdb" -h 127.0.0.1 -p 55440 -U detective_local detective_local
"$DETECTIVE_PG_BIN/createdb" -h 127.0.0.1 -p 55440 -U detective_local detective_test
```

При следующих запусках достаточно pg_ctl start. Для остановки:

```sh
"$DETECTIVE_PG_BIN/pg_ctl" -D "$PWD/.local/pg" stop -m fast
```

## Настройки и миграция

Создайте локальный .env один раз; настоящий секрет/учётная запись не используются.
Команда откажется перезаписать существующий .env. Новый случайный dev-key хранится
локально, не печатается и не коммитится. Не меняйте его при перезапуске: иначе старые
cookie не смогут пройти проверку HMAC.

```sh
.venv/bin/python - <<'PY'
import os
import secrets
from pathlib import Path
content = Path('.env.example').read_text().replace('TOKEN_HASH_PEPPER=\n',
                                                  'TOKEN_HASH_PEPPER=' + secrets.token_hex(32) + '\n')
fd = os.open('.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(content)
PY
set -a
. ./.env
set +a
.venv/bin/alembic upgrade head
.venv/bin/alembic check
.venv/bin/python -m detective.api.catalog docs/examples/demo-case.detective
.venv/bin/python -m detective.api --port 8000
```

Встроенное дело устанавливается явно после миграций. Повторная установка того же
пакета безопасна; подмена содержимого существующего case ID отклоняется.
Локальные файлы БД, каталог пакетов и .env исключены из Git. Сохраняйте их между
рестартами, если нужно продолжить учебную партию.

## Полная проверка через Python-клиенты

В другом терминале (из корня) выставьте PYTHONPATH. API уже работает на 127.0.0.1:8000.
Скрипт держит две независимые cookie-сессии, выполняет полный цикл и печатает только
счёт. Никаких cookie, рук или ключей в stdout.

```sh
.venv/bin/python - <<'PY'
from uuid import uuid4
import httpx

base = 'http://127.0.0.1:8000'
with httpx.Client(base_url=base, headers={'Origin': base}) as host, \
     httpx.Client(base_url=base, headers={'Origin': base}) as guest:
    for client in (host, guest):
        reply = client.post('/api/v1/sessions', json={})
        reply.raise_for_status()
        client.headers['X-CSRF-Token'] = reply.json()['csrf_token']
    reply = host.post('/api/v1/rooms', json={'nickname': 'Ведущий'})
    reply.raise_for_status()
    code = reply.json()['code']
    prefix = f'/api/v1/rooms/{code}'
    guest.post(prefix + '/join', json={'nickname': 'Участник'}).raise_for_status()
    host.post(prefix + '/cases/select', json={'case_id': 'envelope-demo'}).raise_for_status()
    host.post(prefix + '/start', json={
        'client_action_id': str(uuid4()), 'expected_revision': 0,
    }).raise_for_status()
    def view(client):
        reply = client.get(prefix + '/snapshot')
        reply.raise_for_status()
        return reply.json()
    def act(client, kind, payload=None):
        reply = client.post(prefix + '/actions', json={
            'client_action_id': str(uuid4()), 'expected_revision': view(client)['revision'],
            'type': kind, 'payload': payload or {},
        })
        reply.raise_for_status()
    act(host, 'begin_play')
    host_id = host.get(prefix).json()['member_id']
    while view(host)['phase'] == 'PLAYING':
        current = host if view(host)['active_player_id'] == host_id else guest
        card_id = view(current)['private']['hand'][0]['id']
        act(current, 'play_card', {'card_id': card_id, 'mode': 'discard', 'confirmed': True})
    act(guest, 'propose_finish')
    act(host, 'confirm_finish')
    questions = view(host)['questions']
    for question in questions:
        act(guest, 'answer', {'question_id': question['id'], 'option_id': question['options'][0]['id']})
    act(host, 'submit_answers')
    # Human review demonstration: mark all answers incorrect; no automatic comparison with the key.
    for question in questions:
        act(host, 'review_answer', {'question_id': question['id'], 'correct': False})
    act(host, 'finish_game')
    print('FINISHED; score:', view(host)['result']['score'])
PY
```

Это технический smoke-test с учебным сценарием и условной ручной проверкой, не продуктовая
приёмка. Проверка рестарта настоящего процесса входит в автоматические тесты ниже.
Для восстановления пользовательского клиента повторите POST /sessions с сохранённой
cookie, затем GET /rooms/{code}/snapshot; не создавайте новую комнату/партию.

## Тесты и статические проверки

После остановки тестового API можно выполнить:

```sh
export PYTHONPATH="$PWD/backend"
export TEST_DATABASE_URL='postgresql+asyncpg://detective_local@127.0.0.1:55440/detective_test'
.venv/bin/pytest -q
.venv/bin/ruff check backend tests
.venv/bin/ruff format --check backend tests
.venv/bin/mypy backend
.venv/bin/python -m detective.scenarios docs/examples/demo-case.detective
(cd frontend && npm ci && npm run check && npm run lint)
```

Тесты сами мигрируют/удаляют случайные изолированные схемы только в detective_test*.
SQLite и mocks не используются вместо PostgreSQL. Без TEST_DATABASE_URL интеграционные
тесты явно skip; такой запуск не является полной проверкой этапа 2.
CI содержит сервис PostgreSQL, находит google-chrome и запускает весь набор. Настоящий локальный restart-тест
временно поднимает два последовательных Uvicorn-процесса на свободном loopback-порту.

Для проверки миграции вручную на **пустой отдельной тестовой БД**:

```sh
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic check
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic downgrade base
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic upgrade head
```

Downgrade удаляет таблицы приложения: не используйте эту команду на базе сохраняемой партии.


## Проверка WebSocket и двух браузерных сессий

Зависимость websockets включена в requirements-dev.lock. Подключение из браузера:

```js
const ws = new WebSocket(
  `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/v1/rooms/${code}`,
  "detective.v1",
);
```

Cookie HttpOnly браузер приложит сам. Не передавайте токен через URL или JS-поля.
Порядок snapshot/resume/heartbeat и переподключения — WEBSOCKET.md. WS не принимает
команды игры: используйте существующий REST+CSRF. Временно отключённый ведущий
сохраняет роль и руку; новый сокет не запускает новую партию.

Тест настоящих двух изолированных Chrome-контекстов и перезагрузки страницы:

```sh
export DETECTIVE_CHROME_BIN='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
# Linux: укажите путь из command -v google-chrome
.venv/bin/pytest tests/integration/test_browser_sync.py -q
```

Этот тест использует отдельный временный профиль headless Chrome и два независимых
browser context; обычный профиль пользователя не затрагивается. Автоматизация работает
через CDP, без Playwright/Selenium. Возвращаются только безопасные проверки/хеши, без
вывода рук/ключей и без скриншотов. Если DETECTIVE_CHROME_BIN не задан, браузерный тест
явно skip; сетевые WS-тесты продолжают работать при заданном TEST_DATABASE_URL.

Полная проверка этапа 3 требует TEST_DATABASE_URL и DETECTIVE_CHROME_BIN, затем pytest -q.
В ней есть реальный Uvicorn/WebSocket, аварийный рестарт, все фазы игры, presence,
пропущенные ревизии и изоляция комнат. Медленная отправка и короткие heartbeat-таймеры
проверяются отдельно через настоящий FastAPI/PostgreSQL и управляемый ASGI transport;
БД и транзакционный сервис не подменяются.

Для прямого запуска Uvicorn вместо рекомендованного runner обязательны лимиты:

```sh
.venv/bin/python -m uvicorn detective.api.app:create_app --factory \
  --host 127.0.0.1 --port 8000 --workers 1 --no-access-log \
  --ws websockets --ws-max-size 4096 --ws-max-queue 8 --ws-per-message-deflate false
```

Публичное развёртывание и React-интерфейс в этот этап не входят.


## React-интерфейс этапа 4

Интерфейс уже реализован. Команды двух локальных терминалов, настройка Origin для Vite
и полная браузерная проверка — [LOCAL_UI.md](LOCAL_UI.md). Backend по-прежнему запускается
тем же python -m detective.api; отдельный HTTP-импорт/публичное развёртывание не добавлялись.
