# Детектив

Браузерная кооперативная игра. Требования находятся в [docs/README.md](docs/README.md),
постоянные инструкции — в [AGENTS.md](AGENTS.md), аудит — в [docs/AUDIT.md](docs/AUDIT.md).

Репозиторий: [vovelio/detective](https://github.com/vovelio/detective).
История, ветки и переход между версиями: [docs/VERSION_CONTROL.md](docs/VERSION_CONTROL.md).

Выполнены этапы 0–6: контракты, игровое ядро, PostgreSQL, REST/WebSocket, React, общая интерактивная доска и импорт собственных дел.
Полная локальная партия через браузер проверена; спецификация интерфейса — docs/UI_SPEC_FINAL.md.
Запуск интерфейса на macOS: [docs/LOCAL_UI.md](docs/LOCAL_UI.md). Локальный запуск: [docs/LOCAL_API.md](docs/LOCAL_API.md).
Контракты: [ядро](docs/GAME_ENGINE.md), [API и хранение](docs/API.md).

## Локальная проверка (macOS / Linux)

Нужны Python 3.13.3, Node 24.15.0 и npm 11.12.1.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
export PYTHONPATH="$PWD/backend"
# Для полной проверки задайте TEST_DATABASE_URL: см. docs/LOCAL_API.md
.venv/bin/pytest -q
.venv/bin/ruff check backend tests infra
.venv/bin/ruff format --check backend tests infra
.venv/bin/mypy backend
cd frontend
npm ci
npm run check
npm test
npm run lint
npm run build
```

Проверка пакета из корня, без вывода содержимого сценария:

```sh
.venv/bin/python -m detective.scenarios docs/examples/demo-case.detective
```

Схема в `docs/schema/` — контракт; идентичная копия внутри Python-пакета позволяет
проверять сценарии независимо от текущего каталога. Тест запрещает их расхождение.
После изменения зависимостей обновлять `requirements-dev.lock` через `pip freeze --exclude detective`.
Без TEST_DATABASE_URL интеграционные тесты явно пропускаются.
Для браузерного теста задайте DETECTIVE_CHROME_BIN (см. LOCAL_API.md).
Импорт через лобби: [инструкция](docs/IMPORT_GUIDE.md). Публичная инфраструктура остаётся этапу7. Инструкция VPS: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).


Production-подготовка этапа7: [Docker/HTTPS и VPS](docs/DEPLOYMENT.md),
[точные команды и backup/restore](docs/DOCKER_OPERATIONS.md),
[защита и предложение retention](docs/PRODUCTION_SECURITY.md).
Публичный сервер не развёрнут; релизное дело и живая приёмка остаются впереди.
