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
- **Auth**: protect routes with `Depends(get_current_user)` / `require_permission("code")` from `app/modules/auth/deps.py`; permission codes are registered in the owning module via `auth.permissions.register`. `sys_admin` is a superuser: `require_permission` lets it through before checking codes (decision #41 ruling 2; the action is still audited, and the DB-level append-only triggers on `audit_log` are unaffected). Zone scoping — `app/core/abac.py` `zone_filter`. Denied/error audit entries follow the early-commit pattern: write counters + `audit.service.log(..., result="denied")`, `await db.commit()`, then `raise err(...)`. Applicant flows (3.2b): the OneID/E-IMZO/OTP-sender adapters live at `app/modules/integrations/adapters/` since 3.4 (mock/real selected by `*_MODE` env; `app_env=prod` forbids mock); an `applicant`-role user without their own `applicants` row is gated to `/auth/*`'s registration-exempt paths plus `/api/v1/refs/*` only — anything else raises `ERR-AUTH-008` (checked in `get_current_user`, same pattern as must-change-password); legal-entity representations are checked effective on read (`status='active'` and not past `valid_until`, via `business_today()`), and since 3.4 a daily job also flips expired rows to `status='expired'`.
- **Outbox & workers (3.4)**: every outbound message is enqueued via `integrations.service.enqueue(db, destination=..., payload=...)` IN the business action's transaction — never send inside a request. Delivery is the outbox worker's job (retry/backoff → `dead` = the DLQ; admin requeue via `/admin/integrations/*`). Workers (outbox loop + APScheduler scheduler behind a PG advisory lock) run embedded in the API lifespan when `workers_mode=embedded` (default) or standalone via `python -m app.workers [outbox|scheduler]`; tests set `WORKERS_MODE=off` (root conftest). Periodic jobs live in `app/workers/jobs.py` and audit their data changes with `user_id=None`, `correlation_id="job:<uuid>"`. Sender registry: `integrations/senders.py` (`register_sender`). New destinations must never echo the payload into raised exceptions — `last_error` is admin-visible and logged.
- **Rate limiting & idempotency (3.4)**: anonymous auth routes (`/auth/login`, `/auth/otp/request`, `/auth/eimzo/challenge`) carry a per-IP token bucket (`app/core/ratelimit.py`, limits in `system_settings`, 429 `ERR-SYS-006`). Critical POSTs of 3.9/3.10 must attach `auth.deps.idempotency_context` and call `ctx.save(db, status_code=..., body=...)` before returning (mechanism in `app/core/idempotency.py`, 409 `ERR-SYS-005`).
- **Deploy notes (3.4)**: run uvicorn with `--proxy-headers`/`--forwarded-allow-ips` behind a proxy — the rate limiter keys on `request.client.host`, and without them every client shares the proxy's IP (one bucket = a system-wide login outage); with `workers_mode=off` a standalone worker process is mandatory or OTP delivery silently stops; deploying a new `secret_key` (or the 3.4 HMAC switch itself) invalidates in-flight OTP codes (≤5-min TTL — harmless, but know it).
- **Reference data**: read catalogs through `admin.repo`/`admin.service` (or the `/api/v1/refs/*` routes); never re-query `regions`/`organizations`/`classifier_items` from another module's repo. `GET /refs/*` reads with no rule to apply (regions, districts, organizations, activity/livestock types) call `admin.repo` directly from the router — the one router → repo exception to the layering above, made instead of adding an empty pass-through service method. Runtime policy values (session TTLs, lockout thresholds) come from `app/core/settings_store.py` — `get_int(db, "…")`, never from `Settings`. Nothing in reference data is deleted: `status='archived'`, and classifier values are superseded (archive + insert), never rewritten.

## CI

GitHub Actions (`.github/workflows/ci.yml`, stage 3.4): `lint` (ruff check + format check + pyright) and `test` (PostGIS service + MinIO, full pytest incl. the migration downgrade→upgrade round-trip) on every push/PR. Keep both green; the round-trip test runs last and wipes the shared test DB by design (collection order is pinned by a hook in `tests/conftest.py`).
