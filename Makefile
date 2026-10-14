# Docker settings
COMPOSE_BASE = infra/docker-compose.yml
COMPOSE_DEV = infra/docker-compose.override.yml
DOCKER_COMPOSE = docker compose --env-file .env -f $(COMPOSE_BASE)
DOCKER_COMPOSE_DEV = docker compose --env-file .env -f $(COMPOSE_BASE) -f $(COMPOSE_DEV)
DOCKER_COMPOSE_EXEC = $(DOCKER_COMPOSE) exec

# Throwaway PostgreSQL and Redis for the integration suite. Every invocation gets its own compose
# project name and ephemeral host ports so it can never touch the dev stack; all of them are
# built inside the test-integration recipe, which is why there is no shared variable here.
COMPOSE_TEST = infra/docker-compose.test.yml

# Container names
APP_CONTAINER = bot

# Requirements management
REQ_DIR = infra/requirements
REQ_NAMES = base dev prod security
REQ_DEV_TXT = $(REQ_DIR)/dev.txt
REQ_PROD_TXT = $(REQ_DIR)/prod.txt
REQ_COMPILE_IMAGE ?= python:3.13-slim-bookworm
REQ_COMPILE_PLATFORM ?= linux/amd64
REQ_COMPILE_USER = $(shell id -u):$(shell id -g)
REQ_COMPILE_FLAGS ?=

.DEFAULT_GOAL := help

##@ Stack

.PHONY: build
build: ## Build the images
	$(DOCKER_COMPOSE) build

.PHONY: up
up: ## Start the containers
	$(DOCKER_COMPOSE) up -d

.PHONY: run
run: ## Build and start the prod-like stack
	$(DOCKER_COMPOSE) up --build -d

.PHONY: run-dev
run-dev: ## Build and start the dev stack with autoreload
	$(DOCKER_COMPOSE_DEV) up --build -d

.PHONY: down
down: ## Stop and remove the containers
	$(DOCKER_COMPOSE) down

.PHONY: shell
shell: ## Open a bash shell inside the bot container
	$(DOCKER_COMPOSE_EXEC) $(APP_CONTAINER) /bin/bash

##@ Cleanup

.PHONY: clean
clean: ## Remove the stack together with its volumes, local images and orphans
	$(DOCKER_COMPOSE) down -v --rmi local --remove-orphans

.PHONY: clean-resources
clean-resources: ## Remove unused Docker resources, keeping build cache and reusable images
	docker image prune -f
	docker container prune -f
	docker builder prune -f

.PHONY: clean-resources-hard
clean-resources-hard: ## Remove all unused images and build cache, forcing full rebuilds next time
	docker image prune -a -f
	docker container prune -f
	docker builder prune -a -f

##@ Database

.PHONY: migrate
migrate: ## Apply Alembic migrations
	$(DOCKER_COMPOSE_EXEC) $(APP_CONTAINER) alembic upgrade head

.PHONY: migration
migration: ## Create an Alembic revision: make migration m="add users table"
	@MSG="$(m)"; \
	if [ -z "$$MSG" ]; then printf 'Enter migration message: '; read -r MSG; fi; \
	if [ -z "$$MSG" ]; then echo "Migration message cannot be empty"; exit 1; fi; \
	$(DOCKER_COMPOSE_EXEC) $(APP_CONTAINER) alembic revision --autogenerate --message "$$MSG"

.PHONY: psql
psql: ## Open psql inside the postgres container
	$(DOCKER_COMPOSE_EXEC) postgres sh -c 'psql -U $$POSTGRES_USER $$POSTGRES_DB'

.PHONY: redis-cli
redis-cli: ## Open redis-cli inside the redis container
	$(DOCKER_COMPOSE_EXEC) redis redis-cli

##@ Logs

.PHONY: logs
logs: ## Follow logs for every service, or one: make logs s=bot
	$(DOCKER_COMPOSE) logs -f $(s)

##@ Quality

.PHONY: lint
lint: ## Run every pre-commit hook
	pre-commit run --all-files

.PHONY: lint-imports
lint-imports: ## Check the layering contracts in .importlinter
	lint-imports

.PHONY: test
test: ## Run the tests, excluding the integration suite (no Docker needed)
	TESTING=true pytest

.PHONY: test-cov
test-cov: ## Run the tests with a coverage report
	TESTING=true pytest --cov=src --cov-report=term-missing --cov-report=xml

.PHONY: test-integration
test-integration: ## Run the integration suite against a throwaway PostgreSQL and Redis
	@set -e; \
	COMPOSE="docker compose -p tg-bot-template-test-$$$$ --env-file .env.test -f $(COMPOSE_TEST)"; \
	export POSTGRES_PORT= REDIS_PORT=; \
	trap "$$COMPOSE down -v --remove-orphans >/dev/null 2>&1" EXIT INT TERM; \
	$$COMPOSE up -d --wait --wait-timeout 120; \
	PG_PORT="$$($$COMPOSE port postgres 5432 | sed 's/.*://')"; \
	REDIS_TEST_PORT="$$($$COMPOSE port redis 6379 | sed 's/.*://')"; \
	TESTING=true \
	POSTGRES_HOST=127.0.0.1 \
	POSTGRES_PORT="$$PG_PORT" \
	REDIS_HOST=127.0.0.1 \
	REDIS_PORT="$$REDIS_TEST_PORT" \
	REDIS_TEST_INSTANCE="127.0.0.1:$$REDIS_TEST_PORT" \
	pytest tests/integration -m integration -v

.PHONY: test-all
test-all: test test-integration ## Run the unit suite and then the integration suite

##@ Dependencies

.PHONY: req-compile
req-compile: ## Recompile the lockfiles inside a Linux container
	docker run --rm --platform=$(REQ_COMPILE_PLATFORM) \
		-e HOME=/tmp \
		-u $(REQ_COMPILE_USER) \
		-v $(CURDIR):/app \
		-w /app \
		$(REQ_COMPILE_IMAGE) \
		sh -lc 'set -e; python -m pip install --user --no-cache-dir --upgrade pip pip-tools && python scripts/ops/sort_requirements_in.py $(addprefix $(REQ_DIR)/,$(addsuffix .in,$(REQ_NAMES))) && cd $(REQ_DIR) && for name in $(REQ_NAMES); do python -m piptools compile $(REQ_COMPILE_FLAGS) "$${name}.in" -o "$${name}.txt"; done'

.PHONY: req-upgrade
req-upgrade: ## Recompile the lockfiles, bumping every pin to its newest allowed release
	$(MAKE) req-compile REQ_COMPILE_FLAGS=--upgrade

.PHONY: req-sync-dev
req-sync-dev: ## Install the dev lockfile into the active environment
	python -m piptools sync $(REQ_DEV_TXT)

.PHONY: req-sync-prod
req-sync-prod: ## Install the prod lockfile into the active environment
	python -m piptools sync $(REQ_PROD_TXT)

##@ Help

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"} \
		/^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)} \
		/^[a-zA-Z0-9_-]+:.*##/ {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
