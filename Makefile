# Single entry point for the local gate. `make check-all` mirrors CI
# (.github/workflows/ci.yml) exactly. `make check` is only the tests of what
# changed (decision #228): the lint steps run once, in the pre-commit hook at
# `git commit`, and CI runs everything on push.
.PHONY: help install hooks up down logs migrate revision bootstrap seed demo-seed api workers test test-all test-changed lint fmt type security check check-all heads lessons-check

help:               ## List the available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:            ## Install dependencies (core + dev)
	uv sync

hooks:              ## Install the pre-commit git hooks (once per clone)
	uv run pre-commit install

up:                 ## Start the local infra (PostgreSQL 16 + PostGIS, MinIO)
	docker compose up -d

down:               ## Stop the local infra
	docker compose down

logs:               ## Tail the infra logs
	docker compose logs -f

migrate:            ## Apply DB migrations
	uv run alembic upgrade head

revision:           ## Autogenerate a migration — make revision M="add x"
	uv run alembic revision --autogenerate -m "$(M)"

heads:              ## Assert exactly one Alembic head — no DB needed (.claude/lessons.md)
	@uv run python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; \
	h = ScriptDirectory.from_config(Config('alembic.ini')).get_heads(); print('alembic heads:', ', '.join(h)); \
	raise SystemExit(0 if len(h) == 1 else 'FAIL: multiple Alembic heads — run: uv run alembic merge heads -m merge')"

lessons-check:      ## Check .claude/lessons.md structure and size budget
	@uv run python scripts/lessons_check.py

bootstrap:          ## Create the first sys_admin — make bootstrap LOGIN=admin NAME="Admin"
	uv run python -m app.bootstrap --login "$(LOGIN)" --full-name "$(NAME)"

seed:               ## Import reference data — make seed KIND=organizations FILE=path.json
	uv run python -m app.seed "$(KIND)" "$(FILE)"

demo-seed:          ## Fill the DB with demo-sprint data (idempotent) — targets DATABASE_URL
	uv run python -m app.seed.demo

api:                ## Run the API on the host (after `make up`) — http://localhost:8000
	@# Behind a proxy add --proxy-headers --forwarded-allow-ips (the rate limiter keys
	@# on request.client.host). The public QR check's access-log line is dropped by
	@# app/core/logging.py — a proxy in front must exclude that path itself (CLAUDE.md).
	uv run uvicorn app.main:create_app --factory --reload

workers:            ## Run the workers standalone (WORKERS_MODE=off deployments)
	uv run python -m app.workers

fmt:                ## Auto-format and auto-fix
	uv run ruff format .
	uv run ruff check --fix .

lint:               ## Lint + format check, no changes (CI's `lint` job)
	uv run ruff check .
	uv run ruff format --check .

type:               ## Type-check (CI's `lint` job)
	uv run pyright

# `make test` alone is the full suite; `make test M=payments` (or M="permits gis")
# is that module's package only -- the shape to use WHILE WORKING, because the
# full suite costs ~6 min of one shared PostgreSQL and two sessions running it
# at once halve each other (CLAUDE.md "One machine, one make test at a time").
# The full suite still runs on every push in CI and in `make check-all`.
ifdef M
TEST_PATHS = $(addprefix tests/modules/,$(M))
else
TEST_PATHS =
endif

test:               ## Test suite; one module with M=<name> -- make test M=payments
	uv run pytest -q -n 1 --fresh-db $(TEST_PATHS)

test-all:           ## Full test suite (needs `make up`; CI's `test` job)
	# -n 1 locally (2026-09-25, amends decision #92): four workers per run made a
	# solo suite ~4x faster, but this machine runs several sessions at once and
	# each one's `make check` put four more workers on the one PostgreSQL inside
	# a 4-CPU Docker VM -- five sessions were twenty workers and everything
	# crawled. One worker keeps a run on a database of its own (`_gw0`, derived
	# from DATABASE_URL_TEST by tests/conftest.py). CI keeps -n 4: its runner is
	# not this machine. --fresh-db re-creates that DB first: fixtures place
	# random polygons and never clean up, so leftovers from the last run turn
	# into ERR-GIS-002 in fixtures that have nothing to do with geometry.
	# Debugging one file is faster without either flag:
	# uv run pytest tests/modules/<x>/test_y.py
	uv run pytest -q -n 1 --fresh-db

security:           ## Security scan (bandit), same args as CI and pre-commit
	# Run via uvx: bandit is a linter, not an app dependency, so it stays out of
	# pyproject.toml. Version pinned so local and CI report the same findings.
	uvx bandit@1.9.4 -ll --skip B101 -r app

test-changed:       ## Tests of what this branch changed since origin/dev -- make check's test step
	# Two filters (decisions #228, #229). scripts/changed_tests.py bounds the run:
	# a module's package for a change in it, every importer of a changed test
	# helper, the whole suite for anything shared or unknown, nothing for docs.
	# pytest-testmon then runs, inside that bound, only the tests whose executed
	# code changed since its last green run (.testmondata, per worktree; a test
	# it has never seen always runs, so a new worktree's first check is the
	# bound in full). CI runs everything on every push.
	@paths="$$(uv run python scripts/changed_tests.py)"; \
	if [ -z "$$paths" ]; then echo "test-changed: nothing but docs changed since origin/dev -- no tests"; exit 0; fi; \
	if [ "$$paths" = "ALL" ]; then echo "test-changed: shared code changed -- testmon over the whole suite"; paths=""; \
	else echo "test-changed: testmon within $$paths"; fi; \
	uv run pytest -q -n 1 --fresh-db --testmon $$paths

check: test-changed  ## Before a commit: the tests of what changed (lint runs in the commit hook)
check-all: heads lessons-check lint type security test-all  ## Exactly what CI runs, the whole suite included
