# Инфраструктура

Production: compose.yaml, Dockerfiles и Caddy; отдельная тестовая конфигурация —
compose.test.yaml. Операции init/start/backup/restore выполняются infra/ops.py с явным
--env-file и --project. Команды: [DOCKER_OPERATIONS](../docs/DOCKER_OPERATIONS.md).
Топология/первый VPS: [DEPLOYMENT](../docs/DEPLOYMENT.md).
Текущая локальная пользовательская БД не является Docker-тестовым окружением.
