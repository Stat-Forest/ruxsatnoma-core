# Ruxsatnoma backend — conventions

FastAPI modular monolith for the forest-permit system. Architecture, DB schema and API contracts live in the docs repo: `../docs/design/01-struktura-monolita.md`, `02-shema-bd.md`, `03-api-kontrakty.md`; decisions — `../docs/decisions.md` (source of truth).

**Before any work read [`.claude/lessons.md`](.claude/lessons.md)** — the accumulated gotchas of this codebase (`Rule` / `Why` / `How to apply`). If a lesson covers the area you are touching, it overrides your instinct; the entries exist because each one already cost a review round or a production-shaped bug.

## Run / test

`make help` lists every target; **`make check` is the local gate and mirrors CI exactly** (ruff check + format check + pyright + pytest) — run it before every commit, together with `uv run pre-commit run --all-files`. The raw commands behind it:

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
- **Notifications (3.5)**: `notifications.service.notify(db, event_code=..., recipient_user_id=..., params=...)` is how any module talks to a user — call it inside the business transaction, never from a background task of its own; `params` values that are not JSON primitives (a `Decimal` amount, a `date`) are coerced to `str` before storage, so a caller never has to think about the JSONB bind. `inapp` is always written (С19: legally significant notifications reach the cabinet even when other channels are off); `sms`/`email` ride the 3.4 outbox (`sms`/`email` destinations in `integrations/senders.py`) and are never sent synchronously from a request. The `notifications_sms_enabled` ops kill switch means two different things by position: at enqueue time it skips the channel, at DELIVERY time it *raises* (`ChannelDisabled`) so the outbox's backoff ladder PAUSES the queue — only a permanently unreachable recipient may fail a notification and return. Texts live in versioned `notification_templates` rows (admin CRUD under `notifications.templates.manage`, supersede-by-archive, never an in-place rewrite), not in code — the one exception is the OTP text (`integrations/adapters/otp_sender.py::OTP_TEXT`), which has no recipient to pick a language for. A new outbox destination must be registered somewhere a standalone `python -m app.workers` process actually imports (see `app/workers/outbox.py`'s import of `notifications.service`), or delivery dies as `unknown destination` — invisible in every in-process test, total in production.
- **Deploy notes (3.4, extended 3.5)**: run uvicorn with `--proxy-headers`/`--forwarded-allow-ips` behind a proxy — the rate limiter keys on `request.client.host`, and without them every client shares the proxy's IP (one bucket = a system-wide login outage); with `workers_mode=off` a standalone worker process is mandatory or OTP delivery silently stops; deploying a new `secret_key` (or the 3.4 HMAC switch itself) invalidates in-flight OTP codes (≤5-min TTL — harmless, but know it); `PUBLIC_BASE_URL` must be the externally reachable origin or Eskiz's delivery reports go nowhere (a local origin is now rejected outright under `sms_mode=real`); `ESKIZ_CALLBACK_SECRET` must be long and random — it is the only authentication on that endpoint; `sms_mode=real` additionally requires `ESKIZ_EMAIL`/`ESKIZ_PASSWORD`/`ESKIZ_SENDER`, `email_mode=real` requires `SMTP_HOST`/`SMTP_FROM` (plus credentials for an authenticated relay); Cyrillic SMS bill at 70 characters per part, so template edits change cost.
- **Reference data**: read catalogs through `admin.repo`/`admin.service` (or the `/api/v1/refs/*` routes); never re-query `regions`/`organizations`/`classifier_items` from another module's repo. `GET /refs/*` reads with no rule to apply (regions, districts, organizations, activity/livestock types) call `admin.repo` directly from the router — the one router → repo exception to the layering above, made instead of adding an empty pass-through service method. Runtime policy values (session TTLs, lockout thresholds) come from `app/core/settings_store.py` — `get_int(db, "…")`, never from `Settings`. Nothing in reference data is deleted: `status='archived'`, and classifier values are superseded (archive + insert), never rewritten.

## CI

GitHub Actions (`.github/workflows/ci.yml`, stage 3.4): `lint` (single-Alembic-head check + ruff check + format check + pyright + bandit) and `test` (PostGIS service + MinIO, full pytest incl. the migration downgrade→upgrade round-trip) on every push/PR. Keep both green; the round-trip test runs last and wipes the shared test DB by design (collection order is pinned by a hook in `tests/conftest.py`). Git rules — branch/pull/push discipline for the parallel sessions — live in the root `../CLAUDE.md`. In short (decision #47): branch off `dev`, PR back into `dev`; `main` only ever receives `dev` through a release PR; direct commits on either are blocked by `no-commit-to-branch`.

## Continuous learning

After fixing any non-trivial bug or discovering a non-obvious gotcha, **append a
terse entry to `.claude/lessons.md`** — short topic title + **Rule:** / **Why:** /
**How to apply:**, one line each. Ruxsatnoma-specific only; general Python/FastAPI
advice does not belong there. This is part of finishing a task, not optional
polish: closing a stage includes the lessons its reviews produced.

Where a finding belongs:

| Kind of knowledge | Goes to |
|---|---|
| A trap in this code — regex dialects, a partial index, a silent migration | `.claude/lessons.md` |
| An accepted product/design ruling | `../docs/decisions.md` (only after Oybek's explicit OK) |
| What is done and what is next | `../docs/status.md` |
| A question for the customer | `../docs/tz/12-otkrytye-voprosy.md` |
