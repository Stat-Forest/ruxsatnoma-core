# Single entry point for the checks that gate a merge. `check` mirrors the
# nine steps of .github/workflows/ci.yml one for one, in the same order.
# Diverging here is not allowed: the only reason this file exists is so that
# a green `make check` predicts a green CI.

.PHONY: install hooks fmt lint type imports english test security constraints images check up down logs migrate api

install:            ## Install the package with dev dependencies
	pip install -e ".[dev]"

hooks:              ## Install the git pre-commit hooks
	pre-commit install

fmt:                ## Auto-format and auto-fix
	ruff format .
	ruff check --fix .

lint:               ## Check formatting and lint without changing anything
	ruff check .
	ruff format --check .

type:               ## Type-check
	mypy core alembic scripts tests

imports:            ## Verify module boundaries
	lint-imports

english:            ## Verify sources contain no Cyrillic
	git ls-files '*.py' | xargs python scripts/check_english_only.py

test:               ## Run tests. Exit code 5 means nothing was collected yet.
	pytest || [ $$? -eq 5 ]

constraints:        ## Critical constraints hold. Exit code 5 means nothing was collected yet.
	pytest -m integration tests/integration || [ $$? -eq 5 ]

security:           ## Known vulnerabilities
	bandit -r core -ll
	pip-audit

images:             ## Images build — validates the compose definition
	docker compose config --quiet

check: lint type imports english test migrate constraints security images  ## The full gate — same nine steps as CI, same order

up:                 ## Start the local infrastructure
	docker compose up -d

down:               ## Stop the local infrastructure
	docker compose down

logs:               ## Tail the infrastructure logs
	docker compose logs -f

migrate:            ## Apply database migrations
	alembic upgrade head

api:                ## Run the API locally — http://localhost:8000/docs
	uvicorn core.main:app --reload
