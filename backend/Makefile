# Studium data layer.
#
# The offline targets (test-offline, lint) need no database and are what CI
# runs on every push. The rest need `make db-up` first.

PY ?= python
COMPOSE ?= docker compose

DEV_URL     ?= postgresql+psycopg://studium:studium@localhost:5432/studium
# The online tests use the throwaway instance from docker-compose.test.yml,
# so a `make test-db` never touches the development database.
TEST_URL    ?= postgresql+psycopg://studium:studium@localhost:5433/studium_test
MIGRATE_URL ?= postgresql+psycopg://studium:studium@localhost:5433/studium_migrate

export STUDIUM_DATABASE_URL              ?= $(DEV_URL)
export STUDIUM_TEST_DATABASE_URL         ?= $(TEST_URL)
export STUDIUM_MIGRATION_TEST_DATABASE_URL ?= $(MIGRATE_URL)

.DEFAULT_GOAL := help
.PHONY: help install db-up db-down db-wait test-db-up test-db-down migrate \
        downgrade revision seed db-reset test test-offline test-db test-paid \
        lint fmt check migrate-draft

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime and dev dependencies
	$(PY) -m pip install -e ".[dev]"

db-up: ## Start Postgres 16 with pgvector (development)
	$(COMPOSE) up -d db
	@$(MAKE) db-wait

test-db-up: ## Start the throwaway Postgres the online tests use (port 5433)
	$(COMPOSE) -f docker-compose.test.yml up -d test-db
	@until $(COMPOSE) -f docker-compose.test.yml exec -T test-db pg_isready -U studium -d studium_test >/dev/null 2>&1; do sleep 1; done
	@echo "test postgres ready on 5433"

test-db-down: ## Stop it (the data is tmpfs and goes with it)
	$(COMPOSE) -f docker-compose.test.yml down

db-wait: ## Block until Postgres accepts connections
	@$(COMPOSE) exec -T db bash -c \
		'until pg_isready -U studium -d studium >/dev/null 2>&1; do sleep 1; done'
	@echo "postgres ready"

db-down: ## Stop Postgres (keeps the volume)
	$(COMPOSE) down

migrate: ## Apply migrations up to head
	$(PY) -m alembic upgrade head

downgrade: ## Roll back one migration
	$(PY) -m alembic downgrade -1

revision: ## Autogenerate a revision: make revision m="add x to y"
	@test -n "$(m)" || (echo "usage: make revision m=\"add x to y\"" && exit 1)
	$(PY) -m alembic revision --autogenerate -m "$(m)"
	@echo
	@echo "Review it by hand before committing -- autogenerate misses check"
	@echo "constraints, comments and enum evolution (spec 13). Add a"
	@echo "CHANGELOG.md entry, or the migration hygiene test will fail."

seed: ## Populate the demo user and the lambda calculus subject
	$(PY) scripts/seed.py

db-reset: ## Drop, recreate, migrate and seed the dev database
	@echo "resetting $(DEV_URL)"
	@$(COMPOSE) exec -T db psql -U studium -d postgres -v ON_ERROR_STOP=1 -c \
		"DROP DATABASE IF EXISTS studium WITH (FORCE)"
	@$(COMPOSE) exec -T db psql -U studium -d postgres -v ON_ERROR_STOP=1 -c \
		"CREATE DATABASE studium OWNER studium"
	@$(MAKE) migrate
	@$(MAKE) seed

migrate-draft: ## Carry the draft's JSON coursepack forward (dry run)
	$(PY) scripts/migrate_from_draft.py --dry-run

test-offline: ## Schema, enum, migration-hygiene and pure-logic tests (no DB)
	$(PY) -m pytest tests -q -m "not postgres"

test-db: ## Integrity, query-shape and migration-sequence tests (needs a DB)
	$(PY) -m pytest tests -q -m postgres

test: ## Everything except the paid tiers
	$(PY) -m pytest tests -q -m "not anthropic"

test-paid: ## Tier 3: real Voyage calls. Costs money; needs VOYAGE_API_KEY.
	@test -n "$(VOYAGE_API_KEY)" || (echo "VOYAGE_API_KEY is not set" && exit 1)
	STUDIUM_RUN_PAID_TESTS=1 $(PY) -m pytest tests/online/test_retrieval_paid.py \
		-q -m anthropic

lint: ## Ruff
	ruff check studium tests scripts

fmt: ## Ruff format
	ruff format studium tests scripts

check: lint test-offline ## What CI runs before touching a database
