.DEFAULT_GOAL := help

PYTHON ?= python3
LOCAL_ENV := .local/docker-test.env
LOCAL_OPS = $(PYTHON) infra/ops.py --env-file "$(LOCAL_ENV)" --project detective-test-manual --test

.PHONY: help start stop restart-api

help:
	@echo 'make start       — собрать и запустить локальную игру (Docker Desktop должен работать)'
	@echo 'make stop        — остановить игру, сохранив данные'
	@echo 'make restart-api — перезапустить только API'

start:
	@if [ ! -e "$(LOCAL_ENV)" ]; then \
		$(LOCAL_OPS) init --domain localhost --https-port 8443 --http-port 8080 --subnet 172.31.200.0/24 || exit $$?; \
	fi
	@$(LOCAL_OPS) start
	@echo 'Игра запущена: https://localhost:8443'

stop:
	@$(LOCAL_OPS) stop

restart-api:
	@$(LOCAL_OPS) restart-api
