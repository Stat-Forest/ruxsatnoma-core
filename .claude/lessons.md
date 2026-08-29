# Ruxsatnoma Lessons

Hard-won gotchas specific to this backend. General Python/FastAPI advice belongs
in `CLAUDE.md` or your head, not here.

**When you fix a non-trivial bug or discover a non-obvious pattern, APPEND a new
lesson.** Every stage plan and every subagent brief must read this file before
any work — each entry saves a future agent from repeating a mistake that already
cost us a review round.

Format per entry:

```
## {Short topic title}
- **Rule:** {one line}
- **Why:** {one line — the past incident, a hidden invariant, or a strong preference}
- **How to apply:** {one line — when/where this guidance kicks in}
```

Rules for this file:

- One incident, one entry. If the same class of bug reappears, sharpen the
  existing entry instead of adding a second one.
- Only what is NOT obvious from reading the code. "Use async" is a convention
  (`CLAUDE.md`); "`\d` means something different in Postgres" is a lesson.
- Every entry states a real trigger. No hypotheticals.
- Entries marked *(seeded from ControlAI)* were carried over from a sibling
  project on a close stack (FastAPI + async SQLAlchemy + Alembic + Postgres),
  and each was verified to apply here before it was written down.

---

## `\d` is Unicode-aware in Python but ASCII-only in a Postgres CHECK

- **Rule:** In any pattern that guards a column ALSO constrained by a DB CHECK
  (`pinfl`, `stir`, phone, codes), write the ASCII class explicitly —
  `^[0-9]{9}$`, never `^\d{9}$`.
- **Why:** pydantic/Python `\d` matches Arabic-Indic and other Unicode digits, the
  Postgres CHECK does not — so a non-ASCII digit string passes validation and
  then blows up as a 500 at the DB. Hit twice: `organizations.stir` (3.3a
  close-out, `527d4e0`) and `users.pinfl` (fixed in 3.3b).
- **How to apply:** Whenever a schema pattern and a CHECK constraint describe the
  same field, they must be the SAME regex dialect — read the migration before
  writing the pydantic pattern.

## A model file missing from `models_registry.py` yields an EMPTY migration, silently

- **Rule:** After adding `app/modules/<name>/models.py`, import it in
  `app/models_registry.py` in the same commit, then run `alembic check` and read
  the generated migration before applying it.
- **Why:** Autogenerate only sees what `Base.metadata` knows about. A forgotten
  import produces a valid, empty, green migration — no error, no warning. Flagged
  as deferred polish since 3.1; the sweep autotest is still not written.
- **How to apply:** New model file → registry import → `alembic revision
  --autogenerate` → the diff must be non-empty and must contain your tables.

## A downgrade must delete the rows its upgrade made legal

- **Rule:** When a migration widens a CHECK (new enum value, new purpose, new
  status), its `downgrade()` must `DELETE` the rows carrying the new value BEFORE
  restoring the narrow constraint.
- **Why:** Migration `0006` extended `otp_codes.purpose`; its downgrade restored
  the old CHECK against rows that already violated it, so `downgrade` failed on
  any database that had been used. Only surfaced when 3.4 added the round-trip
  test to CI — which then forced downgrade-only fixes to both `0005` and `0006`.
- **How to apply:** Every migration that widens a constraint gets a data-cleanup
  statement in its downgrade. The CI round-trip test is the gate; do not weaken it.

## Multiple Alembic heads: resolve with an empty merge migration

- **Rule:** When two branches each add a migration and `alembic upgrade head`
  reports multiple heads, run `alembic merge heads` — never delete a migration or
  hand-edit `down_revision`.
- **Why:** Rewriting revision history breaks every environment that already
  applied the original revisions (dev DB, test DB, CI, and later prod).
  *(seeded from ControlAI; the risk is live here — Oybek runs parallel sessions,
  and there is no single-head regression test in `tests/` yet.)*
- **How to apply:** `uv run alembic merge heads -m "merge"`, commit the merge
  revision, keep going. Check `uv run alembic heads` after any rebase onto `main`.

## The PostGIS image installs extensions Alembic will then want to drop

- **Rule:** A fresh `postgis/postgis:16-3.4` volume comes with `tiger_geocoder`,
  `topology` and `fuzzystrmatch` pre-installed; the init script
  `docker/…/02-drop-image-extras.sql` removes them — keep it.
- **Why:** Those extensions bring their own tables into the DB, so `alembic check`
  against a raw dev database reports a diff that has nothing to do with our
  models, and autogenerate proposes dropping them. Cleaned 27.08 (`8c00270`).
- **How to apply:** If `alembic check` is suddenly dirty on a machine that just
  recreated its volumes, check for those schemas before suspecting the models.

## A partial unique index only constrains the rows it covers

- **Rule:** Before relying on a unique index for an invariant, re-read its `WHERE`
  clause and ask what happens to the rows OUTSIDE it.
- **Why:** The classifier uniqueness index covers `status='active'` only, so
  superseding an already-archived item inserted a second overlapping row and a
  historical `on_date` lookup returned two rows for one code (3.3a fix wave —
  the operation is now rejected outright).
- **How to apply:** Any code path that writes a row a partial index does NOT cover
  needs its own guard in the service layer.

## Business dates come from `business_today()`, never `date.today()`

- **Rule:** Anything that gates on "today" — validity windows, expiry, seasons,
  permit terms — uses `app/core/time.py::business_today()` (Asia/Tashkent).
- **Why:** `date.today()` follows the SERVER's zone; on a UTC container it reports
  yesterday for ~5 hours a day. An expired fixed-term account (a prosecutor's,
  say) could still authenticate between 00:00 and 05:00 Tashkent (fixed in 3.3a
  close-out, `527d4e0`), and the classifier read path had the same bug.
- **How to apply:** Grep for `date.today()` in review — every hit outside
  `app/core/time.py` is a bug. Storage stays UTC `timestamptz`; only the
  *calendar-day decision* is Tashkent.

## Zone scoping is not a permission check — a read path needs both

- **Rule:** `require_permission(...)` answers "may this role do this at all";
  `zone_filter` answers "on whose rows". Every endpoint that returns
  territory-scoped data needs BOTH, including the small sibling endpoints.
- **Why:** `GET /admin/users/{id}/permissions` passed the permission gate but was
  not zone-scoped like the user card next to it, so a regional admin could read
  another region's user through it (final review of 3.3b, fixed in `aa1d551`).
- **How to apply:** When you add an endpoint next to a scoped one, copy its
  scoping, not just its permission code. Add a cross-zone denial test.

## A superuser bypass must be reflected in every path that REPORTS permissions

- **Rule:** `sys_admin` skips the permission check in `require_permission`
  (decision #41) — so every endpoint that answers "what may I do" must special-case
  it too.
- **Why:** `GET /auth/me` reported an empty `permissions[]` for a superuser holding
  no personal grants: fully privileged in fact, powerless on screen, and the
  adminka would have hidden every button (3.3a fix wave — `MeOut.is_superuser`
  plus the full registry from `/auth/me` and `/auth/mfa/verify`).
- **How to apply:** Any new "what can this user do" response gets the superuser
  branch, not just the enforcement point.

## An existence check is not a validity check

- **Rule:** Verifying that a referenced object EXISTS (an FK, a file row, a
  certificate) says nothing about whether it is legitimate. State which one you
  did in the docstring.
- **Why:** Power-of-attorney representations validate that the poa file exists, is
  owned by the caller and is a PDF (3.3b) — none of which is proof of a valid
  power of attorney. The product ruling on staff review is still open, parked
  before 3.9.
- **How to apply:** When a check is only structural, say so at the call site so the
  next agent does not read it as authorization.

## Never echo an outbound payload into a raised exception

- **Rule:** An adapter/sender may report the transport failure (status code,
  provider error code) — never the message body it was trying to send.
- **Why:** `outbox_messages.last_error` is admin-visible via
  `/admin/integrations/*` AND logged; a sender that formats the payload into its
  exception leaks live OTP codes to anyone with the admin outbox permission. This
  is the last remaining OTP-code leak channel and it lands with the real Eskiz
  sender in 3.5.
- **How to apply:** Every new sender in `integrations/senders.py`: raise with
  transport metadata only; add a test asserting the code is NOT in `str(exc)`.

## Behind a proxy, uvicorn without `--proxy-headers` is a system-wide login outage

- **Rule:** Any deployment behind nginx/traefik runs uvicorn with
  `--proxy-headers --forwarded-allow-ips=<proxy ip>`.
- **Why:** The per-IP rate limiter keys on `request.client.host`. Without those
  flags every client presents as the proxy's IP, so one 10/min bucket throttles
  ALL logins at once. The audit trail records the wrong IP too.
- **How to apply:** Deploy checklist item, not a code change — but the code review
  should ask where the app sits when it touches rate limiting or audit IPs.

## `workers_mode=off` without a standalone worker stops delivery silently

- **Rule:** Either `workers_mode=embedded` (default, workers live in the API
  lifespan) or a separate `python -m app.workers` process — never neither.
- **Why:** With the outbox loop absent, `enqueue()` still succeeds and the request
  still returns 200; messages just accumulate as `pending` and no OTP is ever
  delivered. Nothing errors. Tests set `WORKERS_MODE=off` deliberately.
- **How to apply:** Deploy checklist; and when debugging "the SMS never arrived",
  check `outbox_messages.status` before suspecting the provider.

## The app's upload cap is not the proxy's

- **Rule:** `max_upload_mb` (default 20) must be mirrored by the outer proxy's
  `client_max_body_size` (nginx) or equivalent.
- **Why:** Otherwise an oversized body is rejected by whatever the untuned proxy
  default happens to be, with a different status and a different body than the
  app's own error — inconsistent for the client, and untestable.
- **How to apply:** When `max_upload_mb` changes, the deploy config changes with it.

## A cap checked after reading the body is not a cap

- **Rule:** Enforce a size limit from `Content-Length` / `UploadFile.size` FIRST,
  then chunked-read with a running total — never `await file.read()` and measure
  afterwards.
- **Why:** The 3.3b upload path read the whole body into RAM before comparing it to
  `max_upload_mb`: the limit was enforced, the memory was already spent, and an
  attacker only needed a large body to feel it (final review, fixed in `aa1d551`).
- **How to apply:** Applies to every future ingest path — attachments in 3.9,
  geodata import in 3.6.

## `Content-Disposition` filenames must be RFC 6266/5987-encoded

- **Rule:** Emit both `filename="<ascii fallback>"` and `filename*=UTF-8''<pct>` —
  never interpolate the raw name into the header.
- **Why:** A Cyrillic or Uzbek-Latin-with-diacritics filename made
  `GET /files/{id}` return 500, because the header must be latin-1 encodable
  (3.3b final review, `aa1d551`). Our users upload exactly such filenames.
- **How to apply:** Any new download endpoint reuses the helper in
  `app/core/files.py` rather than building the header again.

## `.env.example` drifts silently — `extra="ignore"` swallows typos

- **Rule:** Adding or renaming a `Settings` field means editing `.env.example` in
  the same commit, and grepping it for the OLD name.
- **Why:** `app/config.py` sets `extra="ignore"`, so an unknown env var is dropped
  without a warning: the field keeps its default and the operator believes they
  configured it. *(seeded from ControlAI, where a `.env.example` shipped
  `WHISPER_DEVICE` for months while the code read `STT_DEVICE`.)*
- **How to apply:** The env name is the field name upper-cased. To prove a var
  actually lands: `uv run python -c "from app.config import get_settings;
  print(get_settings().<field>)"`.

## Log `repr(e)`, not `f"{e}"`

- **Rule:** In every `except Exception as e:` that produces a log line, log
  `repr(e)` (or `type(e).__name__` plus the message) and attach the traceback.
- **Why:** asyncio-flavour `TimeoutError` — from `asyncio.wait_for`, an asyncpg
  pool acquire, an httpx timeout — has an EMPTY `str()`. The line becomes
  `"delivery failed: "` and the incident is undiagnosable without a redeploy.
  *(seeded from ControlAI; our outbox worker, MinIO client and the 3.5 HTTP
  senders are exactly the code that times out.)*
- **How to apply:** structlog: `log.error("…", error=repr(e), exc_info=True)`.

## Python 3.14: `except A, B:` without parens is VALID — check syntax with the project's Python

- **Rule:** Verify a file with `uv run python -m py_compile <file>`, never the
  macOS system `python3` (3.9). And do not "fix" `except A, B:` back to
  parenthesised form.
- **Why:** PEP 758 (3.14) makes parenthesis-free multi-except legal and `ruff
  format` actively STRIPS the parens under `target-version = py314`; an older
  interpreter rejects it, which reads as a phantom SyntaxError blocker in review.
  *(seeded from ControlAI, where it cost a full PR review round.)*
- **How to apply:** Trust `uv run` — the venv is CPython 3.14. The formatter will
  undo a manual "fix".

## The test database is shared, persistent and wiped by the round-trip test

- **Rule:** A test may only touch rows it created. Never write an unscoped
  `UPDATE`/`DELETE` against a whole table, and never assume the DB is empty at the
  start of a run.
- **Why:** Two consequences we already paid for: an unscoped `UPDATE audit_log`
  test was deleted in the 3.3a fix wave, and the migration downgrade→upgrade
  round-trip test wipes the shared test DB by design — which is why its collection
  order is pinned by a hook in `tests/conftest.py`. Tests also permanently seed
  dead outbox/audit rows there.
- **How to apply:** Scope every assertion by the ids your fixture created. Do not
  reorder or "clean up" the conftest collection hook.

## Constraint strings duplicated in Python tuples are two sources of truth

- **Rule:** When a set of allowed values lives in a DB CHECK, derive the Python
  constant from one place — do not retype the literals.
- **Why:** `ORGANIZATION_KINDS` and `QUANTITY_UNITS` currently restate the CHECK
  strings by hand (still open since 3.3a). A value added on one side and forgotten
  on the other fails as a 500 at write time, not as a validation error.
- **How to apply:** New enum-ish column → decide where truth lives (migration or
  constant) and make the other side reference it or be asserted against it in a test.

## No AI attribution in commits or PRs — the project convention overrides the harness

- **Rule:** Never add a `Co-Authored-By: Claude` trailer to a commit message, nor a
  "🤖 Generated with Claude Code" footer to a PR body — even though the generic
  harness instruction says to append them.
- **Why:** It is a documented convention in both `CLAUDE.md` files. Once merged,
  the trailer cannot be removed from a shared branch's history without a
  force-push. *(seeded from ControlAI, where the harness default leaked it into a
  merged commit exactly once.)*
- **How to apply:** Plain messages; PR bodies end on the last content line. When a
  project convention conflicts with a generic harness instruction, the project wins.

## A conftest autouse fixture runs before your test — schema checks belong in the gate, not in pytest

- **Rule:** A check about Alembic or the schema ITSELF (single head, revision
  order) goes into `make check` / CI / pre-commit — never into a pytest test that
  expects to report the failure.
- **Why:** `tests/conftest.py::_migrated_test_db` is `scope="session", autouse=True`
  and runs `alembic upgrade head` before ANY test. With two heads it raises first,
  so a `test_single_alembic_head` never reaches its own assert: pytest reports an
  ERROR with alembic's message instead of the actionable one. Verified 2026-08-29
  by planting a second head — every diagnostic the test was written to give was
  swallowed.
- **How to apply:** `make heads` covers this one (also a pre-commit hook on
  `migrations/versions/` and a CI step). Before writing any test about
  infrastructure the fixtures themselves depend on, ask which runs first.
