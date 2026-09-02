# Single entry point for the local gate. `make check` mirrors CI
# (.github/workflows/ci.yml) exactly, so the two can never drift apart.
.PHONY: help install hooks up down logs migrate revision bootstrap seed api workers test lint fmt type security check heads lessons-check

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

api:                ## Run the API on the host (after `make up`) — http://localhost:8000
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

# WeasyPrint (3.11a) dlopens Pango/GLib/HarfBuzz by leaf name. On Linux the loader finds
# them; on macOS Homebrew's lib dir is not on dyld's default search path, so `import
# weasyprint` fails there and nowhere else. Set as a command PREFIX, never `export`: SIP
# strips DYLD_* from the environment of the /bin/sh make spawns, but not from a variable
# assigned on the command line of the child it launches. Empty on Linux and in CI.
# A bare `uv run pytest` on macOS needs the same prefix — see README / task-2 report.
DYLD := $(if $(filter Darwin,$(shell uname -s)),DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib:/usr/local/lib:$(HOME)/lib:/usr/lib,)

test:               ## Full test suite (needs `make up`; CI's `test` job)
	$(DYLD) uv run pytest -q

security:           ## Security scan (bandit), same args as CI and pre-commit
	# Run via uvx: bandit is a linter, not an app dependency, so it stays out of
	# pyproject.toml. Version pinned so local and CI report the same findings.
	uvx bandit@1.9.4 -ll --skip B101 -r app

check: heads lessons-check lint type security test  ## The full local gate — exactly what CI runs
