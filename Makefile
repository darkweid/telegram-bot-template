COMPOSE_BASE = infra/docker-compose.yml
COMPOSE_DEV = infra/docker-compose.override.yml
DOCKER_COMPOSE = docker compose --env-file .env -f $(COMPOSE_BASE)
# The dev stack always polls, so it never takes the webhook profile from .env:
# a variable set in the shell wins over --env-file.
DOCKER_COMPOSE_DEV = COMPOSE_PROFILES= docker compose --env-file .env -f $(COMPOSE_BASE) -f $(COMPOSE_DEV)
DOCKER_COMPOSE_EXEC = $(DOCKER_COMPOSE) exec

# Throwaway PostgreSQL and Redis for the integration suite. Every invocation gets its own compose
# project name and ephemeral host ports so it can never touch the dev stack; all of them are
# built inside the test-integration recipe, which is why there is no shared variable here.
COMPOSE_TEST = infra/docker-compose.test.yml

# The invoking user, for containers that write into the bind-mounted tree
HOST_USER := $(shell id -u):$(shell id -g)

APP_CONTAINER = bot
# The bot image's name when BOT_IMAGE is unset; matches the default in COMPOSE_BASE.
LOCAL_BOT_IMAGE = tg-bot-template-bot:latest
# The commit baked into a locally built image; /status and /health show it.
# Twelve characters, like the sha- tag CI gives the image.
# Evaluated once; a COMMIT_SHA from the shell or the command line wins.
ifndef COMMIT_SHA
COMMIT_SHA := $(shell git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)
endif
export COMMIT_SHA

# Requirements management
REQ_DIR = infra/requirements
REQ_NAMES = base dev prod security ansible
REQ_DEV_TXT = $(REQ_DIR)/dev.txt
REQ_PROD_TXT = $(REQ_DIR)/prod.txt
REQ_COMPILE_IMAGE ?= python:3.13-slim-bookworm
REQ_COMPILE_PLATFORM ?= linux/amd64
REQ_COMPILE_FLAGS ?=

# Server provisioning (infra/ansible). Its own virtualenv: `make req-sync-dev`
# would uninstall Ansible from the dev environment.
ANSIBLE_DIR = infra/ansible
ANSIBLE_VENV = $(ANSIBLE_DIR)/.venv
ANSIBLE_BIN = $(ANSIBLE_VENV)/bin
ANSIBLE_PLAYBOOK = ANSIBLE_CONFIG=$(ANSIBLE_DIR)/ansible.cfg $(ANSIBLE_BIN)/ansible-playbook -i $(ANSIBLE_DIR)/inventory/$(ENV)
BOOTSTRAP_USER ?= root

.DEFAULT_GOAL := help

##@ Stack

# BOT_IMAGE (in .env or the shell) pins the image CI pushed: it is pulled and run
# as is, because a build would put the checkout's code under the pinned tag.
# `build`, `up` and `run` build the bot image only while the resolved name is
# the local one.
BOT_IMAGE_IS_LOCAL = $(DOCKER_COMPOSE) config --images | grep -qxF '$(LOCAL_BOT_IMAGE)'

.PHONY: build
build: ## Build the images; with BOT_IMAGE set, Postgres only
	@if $(BOT_IMAGE_IS_LOCAL); then \
		$(DOCKER_COMPOSE) build; \
	else \
		echo "BOT_IMAGE is pinned: building Postgres only; make run pulls the bot image"; \
		$(DOCKER_COMPOSE) build postgres; \
	fi

.PHONY: up
up: ## Start the containers; with BOT_IMAGE set, never build the bot image
	@if $(BOT_IMAGE_IS_LOCAL); then \
		$(DOCKER_COMPOSE) up -d; \
	else \
		$(DOCKER_COMPOSE) up -d --no-build; \
	fi

.PHONY: run
run: ## Build and start the prod-like stack; with BOT_IMAGE set, pull it instead
	@if $(BOT_IMAGE_IS_LOCAL); then \
		$(DOCKER_COMPOSE) up --build -d; \
	else \
		$(DOCKER_COMPOSE) pull migrate bot worker scheduler && \
		$(DOCKER_COMPOSE) build postgres && \
		$(DOCKER_COMPOSE) up -d; \
	fi

.PHONY: run-dev
run-dev: i18n-compile ## Build and start the dev stack with autoreload
	$(DOCKER_COMPOSE_DEV) up --build -d

.PHONY: down
down: ## Stop and remove the containers
	$(DOCKER_COMPOSE) down

.PHONY: shell
shell: ## Open a bash shell inside the bot container
	$(DOCKER_COMPOSE_EXEC) $(APP_CONTAINER) /bin/bash

##@ Cleanup

# Let's Encrypt issues a certificate for the same host at most five times a
# week, so the certificate volume outlives `make clean`; remove it by hand
# (`docker volume rm tg-bot-template-certbot-certs`) to start over. compose
# labels every volume it creates with the project name, which selects the rest.
# `rm --volumes` first removes the anonymous volumes images declare (certbot's
# /var/lib/letsencrypt), which `down` without -v would leave behind.
# The project name is fixed in compose, so every checkout and worktree of this
# repository shares these volumes: `make clean` from any of them empties all.
CERTIFICATE_VOLUME = tg-bot-template-certbot-certs
.PHONY: clean
clean: ## Remove the stack with its volumes (the certificate kept), local images and orphans
	$(DOCKER_COMPOSE) --profile webhook rm --stop --force --volumes
	$(DOCKER_COMPOSE) --profile webhook down --rmi local --remove-orphans
	docker volume ls -q --filter label=com.docker.compose.project=tg-bot-template \
		| grep -vxF '$(CERTIFICATE_VOLUME)' | xargs -r docker volume rm

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

# The stack runs the same `migrate` service before the bot starts; this is for
# applying migrations without restarting the bot. It uses the migrations baked
# into the bot image (BOT_IMAGE), so `make build` or a pull refreshes them. The
# dev bot runs `alembic upgrade head` itself on start.
.PHONY: migrate
migrate: ## Apply the bot image's migrations (Postgres is started if needed)
	$(DOCKER_COMPOSE) run --rm migrate

# Runs in the dev image, which carries ruff and black for the post-write hooks in
# alembic.ini. Compose starts Postgres and waits for it, and the database is migrated
# to head first, because autogenerate diffs the models against it. On Linux the
# bind-mounted tree belongs to the host user, and the image's appuser could not write
# the revision there; running as the host user also leaves the file owned by you.
# HOME=/tmp gives black a writable cache directory. The message is quoted
# unexpanded, so quotes, backticks and $ reach alembic as typed.
.PHONY: migration
migration: ## Create an Alembic revision: make migration m="add notes table"
	@MSG='$(subst ','\'',$(value m))'; \
	if [ -z "$$MSG" ]; then printf 'Enter migration message: '; read -r MSG; fi; \
	if [ -z "$$MSG" ]; then echo "Migration message cannot be empty"; exit 1; fi; \
	$(DOCKER_COMPOSE_DEV) run --rm --user "$(HOST_USER)" -e HOME=/tmp -e MSG="$$MSG" $(APP_CONTAINER) \
		sh -c 'alembic upgrade head && alembic revision --autogenerate --message "$$MSG"'

.PHONY: psql
psql: ## Open psql inside the postgres container
	$(DOCKER_COMPOSE_EXEC) postgres sh -c 'psql -U $$POSTGRES_USER $$POSTGRES_DB'

.PHONY: redis-cli
redis-cli: ## Open redis-cli inside the redis container
	$(DOCKER_COMPOSE_EXEC) redis redis-cli

##@ Deployment

DOCKER_COMPOSE_WEBHOOK = $(DOCKER_COMPOSE) --profile webhook

# The first certificate for the webhook host; the certbot service renews it
# from then on. Let's Encrypt fetches the challenge from port 80 of DOMAIN, so
# DNS must already point at this box and port 80 must be open. DOMAIN is the
# host of WEBHOOK_BASE_URL: nginx looks for the certificate under that name,
# and the last command fails when the two differ.
.PHONY: cert-issue
cert-issue: ## Issue the webhook's certificate: make cert-issue DOMAIN=bot.example.com [EMAIL=you@example.com]
	@test -n "$(DOMAIN)" || { echo "Usage: make cert-issue DOMAIN=bot.example.com [EMAIL=you@example.com]"; exit 1; }
	$(DOCKER_COMPOSE_WEBHOOK) up -d --no-deps nginx
	$(DOCKER_COMPOSE_WEBHOOK) run --rm --no-deps --entrypoint certbot certbot certonly \
		--webroot -w /var/www/certbot -d '$(DOMAIN)' --cert-name '$(DOMAIN)' \
		--non-interactive --agree-tos \
		$(if $(EMAIL),--email '$(EMAIL)',--register-unsafely-without-email)
	$(DOCKER_COMPOSE_WEBHOOK) restart nginx
	$(DOCKER_COMPOSE_WEBHOOK) run --rm --no-deps -e NGINX_REQUIRE_TLS=1 nginx nginx -t

# The system python3, not the virtualenv's: the script needs the standard
# library only and runs the same way on the box, as deploy.sh's first step.
.PHONY: check-env
check-env: ## Check .env before a deploy (formats, compose profile, placeholders left in)
	python3 scripts/ops/check_env.py .env

##@ Logs

.PHONY: logs
logs: ## Follow logs for every service, or one: make logs s=bot
	$(DOCKER_COMPOSE) logs -f $(s)

.PHONY: restart
restart: ## Restart every service, or one: make restart s=bot
	$(DOCKER_COMPOSE) restart $(s)

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
	pytest tests/integration -m integration -v

.PHONY: test-all
test-all: test test-integration ## Run the unit suite and then the integration suite

##@ Translations

I18N_DIR = locales
I18N_DOMAIN = messages
I18N_PROJECT = tg-bot-template
I18N_POT ?= $(I18N_DIR)/$(I18N_DOMAIN).pot
# `_`, `gettext`, `ngettext` and `N_` are Babel's defaults; `__` is the alias
# the code uses for lazy_gettext. No locations: line numbers would turn every
# edit above a string into a catalog diff.
I18N_KEYWORDS = -k __ -k lazy_gettext
# Overridable so the catalog tests can point at the interpreter's own pybabel
# when no virtualenv is activated (CI, an IDE's test runner).
PYBABEL ?= pybabel
PYTHON ?= python
# pybabel leaves placeholders and a trailing blank line in the catalogs; the
# script scrubs them so a repeated i18n-update changes nothing. update and init
# remove the .pot whether or not pybabel succeeds.
I18N_NORMALIZE = $(PYTHON) scripts/ops/normalize_catalogs.py

.PHONY: i18n-extract
i18n-extract: ## Extract messages from the code into the .pot template
	$(PYBABEL) extract -F babel.cfg $(I18N_KEYWORDS) --no-location --sort-output \
		--project="$(I18N_PROJECT)" --version=1 -o $(I18N_POT) .

.PHONY: i18n-update
i18n-update: ## Merge new and changed messages into every catalog
	@status=0; \
	$(MAKE) --no-print-directory i18n-extract || status=$$?; \
	if [ $$status -eq 0 ]; then \
		$(PYBABEL) update -i $(I18N_POT) -d $(I18N_DIR) -D $(I18N_DOMAIN) \
			--no-fuzzy-matching --ignore-obsolete --ignore-pot-creation-date || status=$$?; \
	fi; \
	rm -f $(I18N_POT); \
	if [ $$status -eq 0 ]; then $(I18N_NORMALIZE) $(I18N_DIR) --project $(I18N_PROJECT) || status=$$?; fi; \
	exit $$status

.PHONY: i18n-compile
i18n-compile: ## Compile the catalogs into the .mo files the bot loads
	$(PYBABEL) compile -d $(I18N_DIR) -D $(I18N_DOMAIN) --statistics

.PHONY: i18n-init
i18n-init: ## Start a catalog for a new language: make i18n-init LOCALE=de
	@test -n "$(LOCALE)" || { echo "LOCALE is required: make i18n-init LOCALE=de"; exit 1; }
	@status=0; \
	$(MAKE) --no-print-directory i18n-extract || status=$$?; \
	if [ $$status -eq 0 ]; then \
		$(PYBABEL) init -i $(I18N_POT) -d $(I18N_DIR) -D $(I18N_DOMAIN) -l $(LOCALE) || status=$$?; \
	fi; \
	rm -f $(I18N_POT); \
	if [ $$status -eq 0 ]; then $(I18N_NORMALIZE) $(I18N_DIR) --project $(I18N_PROJECT) || status=$$?; fi; \
	if [ $$status -eq 0 ]; then \
		echo "Add \"$(LOCALE)\" to SUPPORTED_LOCALES in src/core/locales.py, then translate and run make i18n-compile"; \
	fi; \
	exit $$status

##@ Dependencies

.PHONY: req-compile
req-compile: ## Recompile the lockfiles inside a Linux container
	docker run --rm --platform=$(REQ_COMPILE_PLATFORM) \
		-e HOME=/tmp \
		-u $(HOST_USER) \
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

##@ Server

.PHONY: ansible-deps
ansible-deps: ## Create the Ansible virtualenv and install the pinned collections
	python3 -m venv $(ANSIBLE_VENV)
	$(ANSIBLE_BIN)/python -m pip install --quiet --upgrade pip
	$(ANSIBLE_BIN)/python -m pip install --quiet -r $(REQ_DIR)/ansible.txt
	@# Galaxy drops connections now and then; three tries before giving up.
	cd $(ANSIBLE_DIR) && for attempt in 1 2 3; do \
		.venv/bin/ansible-galaxy collection install -r requirements.yml && exit 0; sleep 10; \
	done; exit 1

.PHONY: ansible-lint
ansible-lint: ## Lint the playbooks and roles (profile: production)
	cd $(ANSIBLE_DIR) && PATH="$$PWD/.venv/bin:$$PATH" ansible-lint

.PHONY: server-bootstrap
server-bootstrap: ## Prepare a fresh server: make server-bootstrap ENV=production [BOOTSTRAP_USER=root] [BOOTSTRAP_PORT=22022] [ASK_PASS=1] [ASK_BECOME=1]
	@test -n "$(ENV)" || { echo "ENV is required: make server-bootstrap ENV=production"; exit 1; }
	@test -d $(ANSIBLE_DIR)/inventory/$(ENV) || { echo "No inventory at $(ANSIBLE_DIR)/inventory/$(ENV)"; exit 1; }
	$(ANSIBLE_PLAYBOOK) $(ANSIBLE_DIR)/playbooks/bootstrap.yml \
		-e bootstrap_user=$(BOOTSTRAP_USER) \
		$(if $(BOOTSTRAP_PORT),-e bootstrap_port=$(BOOTSTRAP_PORT)) \
		$(if $(ASK_PASS),--ask-pass) \
		$(if $(ASK_BECOME),--ask-become-pass)

.PHONY: server-provision
server-provision: ## Converge a server: make server-provision ENV=production [CURRENT_SSH_PORT=22022] [CHECK=1]
	@test -n "$(ENV)" || { echo "ENV is required: make server-provision ENV=production"; exit 1; }
	@test -d $(ANSIBLE_DIR)/inventory/$(ENV) || { echo "No inventory at $(ANSIBLE_DIR)/inventory/$(ENV)"; exit 1; }
	$(ANSIBLE_PLAYBOOK) $(ANSIBLE_DIR)/playbooks/site.yml \
		$(if $(CURRENT_SSH_PORT),-e bootstrap_port=$(CURRENT_SSH_PORT)) \
		$(if $(CHECK),--check --diff)

.PHONY: server-reboot
server-reboot: ## Reboot a server and wait for the stack: make server-reboot ENV=production
	@test -n "$(ENV)" || { echo "ENV is required: make server-reboot ENV=production"; exit 1; }
	@test -d $(ANSIBLE_DIR)/inventory/$(ENV) || { echo "No inventory at $(ANSIBLE_DIR)/inventory/$(ENV)"; exit 1; }
	$(ANSIBLE_PLAYBOOK) $(ANSIBLE_DIR)/playbooks/reboot.yml

##@ Help

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"} \
		/^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)} \
		/^[a-zA-Z0-9_-]+:.*##/ {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
