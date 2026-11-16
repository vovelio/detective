# Локальный запуск React-прототипа на macOS

Нужны Python 3.13.3, Node 24.15.0, npm 11.12.1, PostgreSQL 18. Сервисы запускаются
только на 127.0.0.1. Caddy/Docker/VPS на этом этапе не требуются.

## Один раз: база, настройки и зависимости

Если локальная БД и .env ещё не созданы, выполните разделы «Отдельный локальный
PostgreSQL» и «Настройки и миграция» в [LOCAL_API.md](LOCAL_API.md). Там приведены
команды initdb/createdb и генерации одноразового dev-key в .env без вывода секрета.
Не используйте настоящие данные или существующую рабочую БД.

Из корня проекта:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
npm --prefix frontend ci
```

## Терминал 1 — API

Если тестовый кластер по LOCAL_API.md уже создан, запустите его при необходимости:

```sh
export DETECTIVE_PG_BIN='/Library/PostgreSQL/18/bin'
"$DETECTIVE_PG_BIN/pg_ctl" -D "$PWD/.local/pg" -l "$PWD/.local/postgres.log" \
  -o "-h 127.0.0.1 -p 55440 -k $PWD/.local" start
```

Затем в том же терминале, из корня:

```sh
export PYTHONPATH="$PWD/backend"
set -a
. ./.env
set +a
export ALLOWED_ORIGINS='http://127.0.0.1:5173,http://127.0.0.1:8000'
export SESSION_COOKIE_SECURE=false
.venv/bin/alembic upgrade head
.venv/bin/python -m detective.api.catalog docs/examples/demo-case.detective
.venv/bin/python -m detective.api --port 8000
```

При рестарте сохраняйте прежние .env/pepper, PostgreSQL и CASE_STORAGE_PATH.
Повтор установки того же demo безопасен; новую партию он не создаёт.

## Терминал 2 — интерфейс

Из корня:

```sh
npm --prefix frontend run dev
```

Откройте [http://127.0.0.1:5173](http://127.0.0.1:5173).
Vite проксирует /api и /ws в API на 8000, сохраняя исходные Origin/Host и cookie.
Не меняйте адрес на localhost без соответствующей настройки ALLOWED_ORIGINS:
для cookie/WS важен один согласованный origin.

В первом браузере создайте комнату. Скопируйте приглашение и откройте его в другом
браузере или отдельном приватном контексте. Обычные вкладки одного профиля используют
одну cookie и являются тем же игроком — это ожидаемое поведение.

Ведущий выбирает учебное дело и запускает расследование. Пройдите вступление,
публикуйте/сбрасывайте карты, перейдите к командным вопросам и ручной проверке,
посмотрите результат и нажмите «Завершить партию». Попробуйте F5 и временное отключение
сети: личность и состояние должны восстановиться. После ошибки неоднозначного запроса
нажимайте «Повторить сохранённый запрос», а не создавайте второй ход.

Сборку можно проверить отдельно:

```sh
npm --prefix frontend run build
npm --prefix frontend run preview
```

Preview — также локальная проверка на 5173 с тем же proxy; dev и preview одновременно
на одном порту не запускаются. Это не релизное развёртывание.

## Автоматические проверки

При запущенной отдельной тестовой базе из LOCAL_API.md:

```sh
export PYTHONPATH="$PWD/backend"
export TEST_DATABASE_URL='postgresql+asyncpg://detective_local@127.0.0.1:55440/detective_test'
export DETECTIVE_CHROME_BIN='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
npm --prefix frontend run check
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
.venv/bin/pytest -q --tb=short
.venv/bin/ruff check backend tests
.venv/bin/ruff format --check backend tests
.venv/bin/mypy backend
.venv/bin/python -m detective.scenarios docs/examples/demo-case.detective
```

E2E сами создают отдельные схемы PostgreSQL, поднимают Uvicorn/Vite на свободных
loopback-портах и используют временные Chrome-профили. Никаких live-данных или личного
профиля браузера. Без TEST_DATABASE_URL/Chrome соответствующие тесты явно skip;
такой запуск не является полной проверкой этапов 4–5.

Только UI E2E:

```sh
.venv/bin/pytest tests/integration/test_ui.py -q --tb=short
```

Полная автоматизированная партия завершается до FINISHED через реальные DOM-контролы.
Во время неё не вызываются игровые API-команды в обход UI. CDP используется для
нажатий/ввода, перезагрузки, временного offline и потери HTTP-ответа после COMMIT.
Подробности реализации и оставшиеся ограничения: FRONTEND.md.

Для остановки UI/API нажмите Ctrl+C в их терминалах. Отдельный учебный PostgreSQL:

```sh
"$DETECTIVE_PG_BIN/pg_ctl" -D "$PWD/.local/pg" stop -m fast
```


## Доска этапа 5

Перед запуском обновлённого API обязательно `.venv/bin/alembic upgrade head` с прежним
.env/pepper. Миграция добавляет board_objects, не создаёт новую партию/личность.
Локальный кластер, созданный в этом чате после этапа4, находится в `.local/pg-local`
(в примерах выше путь `.local/pg` — для новой установки); используйте свой существующий
путь, не выполняйте initdb повторно поверх данных.

Потяните свою карту в выбранную точку доски и подтвердите; для сброса — в маленькую
стопку справа. Подтверждение публикации выключается в шестерёнке; сброс всегда с
подтверждением. Чужие руки проверяйте через отдельный браузер/профиль/incognito.

Пан — пустой участок, «Рука» — любая точка; зум — колесо/кнопки. «Выбор» двигает
публичные карты; после выбора карты доступна ручка вращения. «Стрелка»/«Карандаш» —
протягивание на поле, «Заметка» — клик и текст, «Ластик» — клик по аннотации.
Карта/камера имеют фиксированный размер объекта/локальный масштаб соответственно.
Проверьте вторым игроком live-движение до отпускания, сохранение после отпускания,
F5 и возврат после offline. При конфликте дождитесь обновления и повторите правку явно.
Публичный доступ, импорт сценариев и реальная партия на VPS остаются следующим этапам.


## Импорт этапа 6

После обновления кода установите зафиксированные Python-зависимости и примените head:

```sh
.venv/bin/python -m pip install -r requirements-dev.lock
export PYTHONPATH="$PWD/backend"
set -a
. ./.env
set +a
.venv/bin/alembic upgrade head
.venv/bin/alembic check
```

Затем запустите API и Vite как выше. В лобби ведущему доступна «Добавить свою игру»:
выберите .detective, нажмите «Загрузить дело», затем выберите добавленное дело в списке.
Для быстрой проверки подойдёт docs/examples/demo-case.detective. Все шаги подготовки,
проверки CLI, упаковки на macOS и восстановления после потери ответа —
[IMPORT_GUIDE.md](IMPORT_GUIDE.md). Собственный сценарий виден только этой комнате.

Новая миграция c6e20a4f9137 сохраняет прежние комнаты/партии/сессии. Удалять .env,
PostgreSQL или CASE_STORAGE_PATH не нужно. Без архива ресурсов одного бэкапа PostgreSQL
недостаточно. Downgrade при наличии импортированных дел намеренно отклоняется;
не удаляйте пользовательские дела ради отката.

Полный тест по-прежнему выполняется на отдельной detective_test* с Chrome; добавлены
HTTP-проверки ZIP/прав/конкурентности и настоящая браузерная загрузка с полной партией.
Публичное размещение и эксплуатационная инфраструктура этапа7 не реализованы.


## Отдельный контейнерный HTTPS-стенд этапа7

Нативный запуск выше остаётся удобным dev-режимом. Production-проверка использует
отдельные Docker-проект/порты/тома и не требует удаления `.local/pg-local` или текущих
комнат. Установите и запустите Docker Desktop, затем следуйте разделу «Новый локальный
TLS-стенд» в DOCKER_OPERATIONS.md. На Mac без docker в PATH:

```sh
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
```

Полная автоматизированная проверка контейнеров запускается явно:

```sh
DETECTIVE_DOCKER_TESTS=1 DETECTIVE_CHROME_BIN='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' .venv/bin/pytest -q tests/production --tb=short
```

Без opt-in контейнерный E2E явно skip; это не считается проверкой Docker/HTTPS/restore.
Он создаёт и уничтожает только собственные detective-test-UUID проекты/тома, используя
временный Chrome-профиль и точный сертификат тестового Caddy. Глобальное доверие ОС
к тестовому CA не меняется. Существующий локальный сайт/БД не используются этим тестом.
