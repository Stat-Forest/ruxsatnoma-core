# Ruxsatnoma backend — conventions

FastAPI modular monolith for the forest-permit system. Architecture, DB schema and API contracts live in the docs repo: `../docs/design/01-struktura-monolita.md`, `02-shema-bd.md`, `03-api-kontrakty.md`; decisions — `../docs/decisions.md` (source of truth).

## Run / test

```bash
uv sync
docker compose up -d          # PostgreSQL 16 + PostGIS, MinIO; test DB auto-created
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload
uv run pytest -v              # integration tests need docker up
uv run ruff check . && uv run ruff format --check .
uv run pyright                # type check (standard mode, decision #39)
uv run python -m app.seed organizations app/seed/data/organizations.example.json   # reference data
```

## Hard rules

- **English everywhere** in code, comments, docstrings, commit messages (decision №35). Existing Russian comments from the stage-2 skeleton stay until touched. No AI attribution in commits.
- **Async only** (№26): asyncpg, SQLAlchemy 2 async, httpx; no sync drivers, no `time.sleep`.
- **Module boundaries** (design/01): `app/modules/<name>/` with `router → service → repo → models` (+ `schemas`). Cross-module calls only via the other module's `service`; upward signals via synchronous in-transaction events; readers (reports, dashboard, search, oversight, archive) plus gis/norms-over-permits get read-only table access. `app/core/` never imports domain modules.
- **Transactions**: `get_db` commits on success, rolls back on exception (№37). Services may commit earlier explicitly.
- **Errors**: raise only via `err("ERR-…")` from `app/core/errors.py`; the catalog mirrors `../docs/tz/10-klassifikatory.md`. Single response format with `correlation_id` — do not invent ad-hoc error bodies.
- **Time**: store UTC (`timestamptz`), display Asia/Tashkent. Money/norms — `numeric`, never float.
- **Migrations**: Alembic autogenerate is wired with GeoAlchemy2 `alembic_helpers` (protects PostGIS tables) — do not remove; keep `sqlalchemy.url` in `alembic.ini` empty (URL comes from settings/attributes); naming convention lives on `Base.metadata`. A guard test asserts an empty autogenerate diff.
- **Audit invariant**: every state-changing action calls `audit.service.log(db, action=…)` in the same transaction (no commit inside — `get_db` commits both together). Action codes: `"<object>.<verb>"` in English, constants live in the acting module.
- **Auth**: protect routes with `Depends(get_current_user)` / `require_permission("code")` from `app/modules/auth/deps.py`; permission codes are registered in the owning module via `auth.permissions.register`. `sys_admin` is a superuser: `require_permission` lets it through before checking codes (decision #41 ruling 2; the action is still audited, and the DB-level append-only triggers on `audit_log` are unaffected). Zone scoping — `app/core/abac.py` `zone_filter`. Denied/error audit entries follow the early-commit pattern: write counters + `audit.service.log(..., result="denied")`, `await db.commit()`, then `raise err(...)`.
- **Reference data**: read catalogs through `admin.repo`/`admin.service` (or the `/api/v1/refs/*` routes); never re-query `regions`/`organizations`/`classifier_items` from another module's repo. `GET /refs/*` reads with no rule to apply (regions, districts, organizations, activity/livestock types) call `admin.repo` directly from the router — the one router → repo exception to the layering above, made instead of adding an empty pass-through service method. Runtime policy values (session TTLs, lockout thresholds) come from `app/core/settings_store.py` — `get_int(db, "…")`, never from `Settings`. Nothing in reference data is deleted: `status='archived'`, and classifier values are superseded (archive + insert), never rewritten.

## Next-work checklist (stage 3 start)

1. CI pipeline — deferred until the repos get a remote (pyright is done: decision #39, wired into pre-commit and the commands above).
