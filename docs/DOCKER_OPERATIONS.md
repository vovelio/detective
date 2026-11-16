# Docker: изолированная проверка и операторские процедуры

Публичный сервер не разворачивается этими инструкциями автоматически. Production-
топология и первое размещение описаны в DEPLOYMENT.md. Здесь — точный контракт
локального инструмента `infra/ops.py`. Нужны Docker Engine/Desktop с Compose v2,
Python3.13 и доступ к официальному registry при первой сборке. На macOS Docker Desktop
должен быть запущен; CLI также обнаруживается по стандартному пути приложения.

## Файлы и образы

- compose.yaml: proxy/api/db; migrate и cases-init — явные одноразовые операции.
- compose.test.yaml: исключительно локальный TLS стенд, bind127.0.0.1, внутренний CA.
- backend/Dockerfile: один non-root процесс FastAPI UID10001, Python3.13.18.
- frontend/Dockerfile: Node24.15.0 build → Caddy2.11.9 со статическим /srv.
- PostgreSQL18.6; volume `/var/lib/postgresql`, PGDATA `/var/lib/postgresql/18/docker`.
- Все четыре базовых образа зафиксированы tag+digest, существование проверено HEAD
  официального Docker Registry. Runtime dependencies взяты из зафиксированного production lock без инструментов
  разработки. Уязвимые старые Python-пакеты обновлены при security-review этапа7;
  совместимые версии и результаты OSV-проверки — PRODUCTION_SECURITY.md.

Приватная application-сеть отделена от edge-сети Caddy. У API и PostgreSQL нет host
ports. По умолчанию Caddy имеет application-IP172.30.0.2; только этот адрес доверен
для forwarded headers. Для независимых стендов задавайте непересекающиеся private/24.
NETWORK_DYNAMIC_RANGE автоматически задаёт верхнюю половину сети (.128/25), поэтому
динамические DB/API адреса не занимают статический адрес proxy.
Caddy передаёт исходный Host, переопределяет X-Forwarded-For/Proto и не доверяет
входящим клиентским значениям. Не подключайте CDN/другой proxy без отдельного изменения
модели доверия.

## Секреты

`init` никогда не перезаписывает существующие файлы. Он создаёт публичный env-файл
и соседний каталог `<env-stem>.secrets`: database_url, postgres_password,
token_hash_pepper. Переменные Compose содержат только пути `_FILE`, не значения.

Каталог секретов принадлежит оператору и имеет режим0700; postgres_password —0600.
Файлы database_url и token_hash_pepper имеют0444 **внутри закрытого каталога0700**:
это необходимо non-root API при file-backed Docker Compose secrets, где uid/mode
не переназначаются как в Swarm. Другие пользователи хоста не могут пройти каталог,
в контейнер файлы монтируются только тем сервисам, которым нужны. Backup-копии всех
секретов —0600. Инструмент отказывается работать с открытым/чужим каталогом секретов.

Держите env/secret-файлы под `.local/` либо вне репозитория. `.dockerignore` исключает
`.env*`, `.local`, `*.secrets`, архивы и backup; не помещайте секреты в произвольные
другие файлы build context. Не выводите `docker inspect` окружения старых ручных
контейнеров/содержимое файлов. Ключ pepper должен сохраняться между обновлениями.

## Новый локальный TLS-стенд

Из корня репозитория:

```sh
python3 infra/ops.py --env-file "$PWD/.local/docker-test.env" \
  --project detective-test-manual --test init \
  --domain localhost --https-port 8443 --http-port 8080 --subnet 172.31.200.0/24

python3 infra/ops.py --env-file "$PWD/.local/docker-test.env" \
  --project detective-test-manual --test start
```

`start`: сборка → остановка API/proxy выбранного проекта → healthy PostgreSQL →
права case volume → `alembic upgrade head` → healthy API/proxy. Миграции не исполняются
автоматически на restart контейнера. Bootstrap создаёт отдельную роль detective_app
(NOSUPERUSER/NOCREATEROLE/NOCREATEDB/NOREPLICATION), владельца БД/схемы. API и Alembic
используют её отдельный пароль из DATABASE_URL; административный POSTGRES_USER/PASSWORD
используются только для bootstrap/dump/restore. Пароли передаются psql через stdin с
отключённым логированием SQL этой административной сессии. При отказе миграции приложение остаётся
остановленным, чтобы новая версия не принимала трафик на несовместимой схеме.

Локальный адрес `https://localhost:8443`. Caddy выпускает локальный тестовый сертификат;
ОС/обычный браузер ему ещё не доверяют. Инструмент **не устанавливает CA глобально**.
Для автоматического теста используется отдельный Chrome-профиль с точным SPKI pin
сертификата. Корневой CA доступен из test volume:

```sh
docker compose --env-file "$PWD/.local/docker-test.env" -p detective-test-manual \
  -f compose.yaml -f compose.test.yaml cp \
  proxy:/data/caddy/pki/authorities/local/root.crt "$PWD/.local/docker-test-ca.crt"
```

CA передавайте HTTP-клиентам как конкретный trust file, не используйте глобальное
отключение проверки сертификатов. Ручная пользовательская игра остаётся на прежнем
http://127.0.0.1:5173; этот стенд отдельный.

Другие команды с тем же префиксом `--env-file … --project … [--test]`:

- `build`: собрать образы, не запускать приложение;
- `restart-api`: перезапустить один API, без миграции/повторной раздачи;
- `stop`: остановить proxy/api/db, сохранив тома;
- `destroy-test`: удалить контейнеры и тома **только** при `--test` и имени проекта
  `detective-test-*`. Команда не подходит для production и не используется на текущей
  локальной пользовательской базе.

## Резервное копирование

```sh
python3 infra/ops.py --env-file "$PWD/.local/docker-test.env" \
  --project detective-test-manual --test backup "$PWD/.local/backups/checkpoint-01"
```

Назначение должно отсутствовать. Пока API и proxy остановлены, выполняются pg_dump
(custom format) и копирование case volume, Caddy TLS data/config и секретов. Так один
checkpoint согласует состояние БД и пакеты. PostgreSQL остаётся запущен; все другие
писатели/операторские CLI должны быть остановлены на время backup. Операции ops.py
с одним env сериализуются неблокирующим maintenance-lock; чужой ручной CLI этим lock
не управляется и остаётся ответственностью оператора. Ранее работавшие
API/proxy возвращаются в `finally`. Это короткое окно обслуживания, а не hot backup.

Результат — закрытый каталог0700:

```text
manifest.json       # version/time/project/schema head/image IDs/SHA256 файлов
public.env
secrets/            # pepper, DB password, database URL; файлы0600
database.dump
cases.tar           # private validated packages, без незавершённых .upload-*
caddy_data.tar      # в том числе private keys/локальный тестовый CA
caddy_config.tar
```

Содержимое секретное. Перед переносом вне доверенного хоста зашифруйте backup
утверждённым операторским средством и храните ключ отдельно. Встроенного шифрования
или отправки в облако нет. Одних файлов БД без case archive/pepper недостаточно.
Исходный код/образ приложения не входят в dump: храните release commit, Dockerfiles,
зафиксированные образы и deployment configuration вместе с операторской историей.

`manifest` проверяет повреждения, не подлинность. ACME_EMAIL — необязательное операторское
поле публичной конфигурации; текущая Caddy-конфигурация использует автоматический
аккаунт без явно заданного contact email. Restore допускает **только доверенные
собственные backup**: PostgreSQL dump по природе может содержать SQL. TAR предварительно
проверяется на абсолютные пути, `..`, ссылки и специальные файлы; на хост не извлекается.

## Проверка восстановления

Создайте **новый** проект/новые env/secrets/непересекающуюся сеть, не используйте
действующие тома. Сначала `init`, затем `restore`:

```sh
python3 infra/ops.py --env-file "$PWD/.local/docker-restore.env" \
  --project detective-test-restore --test init \
  --domain localhost --https-port 8444 --http-port 8081 --subnet 172.31.202.0/24

python3 infra/ops.py --env-file "$PWD/.local/docker-restore.env" \
  --project detective-test-restore --test restore "$PWD/.local/backups/checkpoint-01"
```

Restore отказывается работать, если в целевом проекте уже есть контейнеры или тома.
Он сохраняет целевые домен/порты/сеть, восстанавливает исходный pepper и DB password,
пересобирает внутренний DATABASE_URL для целевого DB name и detective_app, восстанавливает дамп,
архивы и TLS-данные, выполняет управляемую миграцию и запускает API/proxy.
При смене origin браузерная cookie автоматически не переносится: для проверки
той же сессии нужен тот же origin либо управляемый тест с сохранённой cookie в памяти.
Тестовая проверка destroy/restore использует прежний origin и восстановленный Caddy CA.

Не используйте `down -v` на production ради восстановления. Для реального аварийного
переключения подготовьте новый сервер/проект, проверьте восстановление, затем отдельно
согласуйте переключение трафика. Автоматического downgrade БД нет; миграция импорта
отказывается откатываться при существующих импортированных делах.

## Обновление

Сначала проверенный backup, затем checkout релиза/сборка, затем `start`. Образы должны
быть версионными (`IMAGE_TAG`), а не случайным mutable local-тегом для публичного релиза.
При изменении schema сверяйтесь с release notes. Возврат старого кода на новую схему
разрешён только при доказанной совместимости; иначе восстановление в новом окружении.
