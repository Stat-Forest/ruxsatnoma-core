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
- **And the opposite failure:** an EMPTY value is not ignored, it is parsed.
  `EMAIL_MODE=`/`SMTP_PORT=`/`SMTP_STARTTLS=` fail validation outright and
  `ESKIZ_BASE_URL=` silently replaces a working default with `""`, so
  `cp .env.example .env` — the README's first step — would not start (3.5 final
  review). Every line in that file carries a real value or is commented out;
  `tests/test_config.py::test_env_example_is_a_working_env_file` builds `Settings`
  from a copy of it and is the guard.

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

## pre-commit refuses to run while `.pre-commit-config.yaml` is modified-but-unstaged

- **Rule:** Never edit `.pre-commit-config.yaml` (or any tooling file) in a working
  copy another session is committing from — take a worktree, per the root
  `CLAUDE.md`. If pre-commit says *"Your pre-commit configuration is unstaged"*,
  the fix is to find whose edit it is, not to reach for `--no-verify`.
- **Why:** pre-commit refuses to run against a config it cannot trust, so EVERY
  commit in that tree is blocked — including commits from a session that never
  touched the file. Hit twice on 2026-08-29 in the shared copy: a tooling session
  had the config modified while the stage-3.5 session was committing, and that
  session shipped one commit with `--no-verify` (hooks re-run by hand) to get out.
- **How to apply:** One worktree per session (root `CLAUDE.md` → Git). If you are
  already stuck mid-task, `git stash push .pre-commit-config.yaml` in your own
  tree is safer than `--no-verify`; if you do use `--no-verify`, run `make check`
  by hand and say so in the PR.

## An outbox sender's return/raise choice IS the retry decision

- **Rule:** A sender registered with `register_sender` must RETURN when a
  failure is permanent (nothing will fix itself by retrying) and RAISE only
  when a retry could plausibly help — the worker (`deliver_one`) retries on any
  raised exception and treats a normal return as delivered.
- **Why:** `notifications._deliver` sets the row `failed` and returns instead
  of raising when the recipient has no verified phone/e-mail; raising there
  would retry an unreachable recipient up to `outbox_max_attempts` times for
  the exact same outcome, burning the circuit breaker's failure count and
  holding back every other queued message on that destination for nothing.
- **How to apply:** Before writing `raise` in a sender, ask "would a second
  attempt with the same input succeed?" If no, set the terminal status
  yourself and `return`.
- **The mirror-image bug, hit in 3.5's final review:** the same helper answered
  both "is this recipient reachable" (permanent) and "is the ops kill switch on"
  (temporary), and `_deliver` treated both as permanent — so flipping
  `notifications_sms_enabled` off for an hour DESTROYED every queued SMS with a
  reason blaming the recipient, unrecoverably (admin requeue only works on `dead`
  rows). A condition an operator can reverse must RAISE. Never let one boolean
  stand for a permanent and a temporary reason at once.

## `str.format` on admin-authored text is an attribute-access hole

- **Rule:** Never render user/admin-authored template text with `str.format`
  or an f-string; substitute placeholders with a whitelist regex
  (`\{([a-z][a-z0-9_]*)\}`) instead.
- **Why:** `"{x.__class__}".format(x=obj)` reaches Python attributes on
  whatever object is passed in, and `.format()` raises `KeyError` on a
  placeholder the caller forgot to supply — inside a business transaction that
  turns an admin's typo into a failed application submission, not a rendering
  glitch.
- **How to apply:** Any text a non-developer can author and the code later
  renders (notification templates today; announcements, rejection reasons or
  report labels tomorrow) goes through `notifications.service.render`'s
  pattern, never `.format(**params)`.

## A partial unique index needs a `flush()` between the archive and the insert

- **Rule:** When "supersede" means archive-the-old-row then insert-a-new-
  active-one under a partial unique index (`WHERE status='active'`), `flush()`
  after the archive UPDATE and before the new INSERT.
- **Why:** Without the flush, the UPDATE and the INSERT are both still pending
  in the same transaction when the index has to be checked — the old row has
  not yet been "seen" as archived, so the insert can raise `IntegrityError` on
  a conflict a flush would already have resolved.
- **How to apply:** Every supersede-shaped write (classifier items since
  3.3a, notification templates since 3.5) follows `old.status = "archived"`,
  `await db.flush()`, then `db.add(new_row)` — copy that order, not just the
  two statements.

## Senders registered at module import must be imported by the standalone worker too

- **Rule:** A destination registered via `register_sender(...)` at module
  import time (e.g. inside `notifications/service.py`) only exists in a
  process that actually imported that module — add the import to
  `app/workers/outbox.py` explicitly, with a comment, in the same commit that
  adds the destination.
- **Why:** In every test and in the embedded-worker deployment, `app.main`
  imports the routers, which transitively import `notifications.service`, so
  registration always "just happens" — a standalone `python -m app.workers`
  process imports neither, so without the explicit import every notification
  goes `dead` as `unknown destination`, and no in-process test can ever catch
  it (only a subprocess test that imports `app.workers.outbox` alone can).
- **How to apply:** New outbox destination → grep `app/workers/outbox.py` for
  the registering import → add it if missing → write or extend the subprocess
  registration test.

## A parsed form body can hold non-str values that a JSONB column cannot

- **Rule:** Before storing a `request.form()` dict as JSON(B), coerce every
  value to `str` (or a short type marker) — never assume form fields are
  strings.
- **Why:** A `multipart/form-data` file part parses to Starlette's
  `UploadFile`, not a string; `json.dumps` cannot serialize it, so an
  untouched form dict reaching a JSONB bind (the Eskiz callback's dead-letter
  payload) 500'd on a one-line curl against an anonymous, internet-facing
  route — exactly the case the route exists to survive.
- **How to apply:** Any endpoint that accepts `request.form()` from an
  untrusted or anonymous caller and persists the result:
  `{k: v if isinstance(v, str) else f"<{type(v).__name__}>" for k, v in
  form.items()}`, never a bare dict comprehension.

## An in-place UPDATE leaves an `onupdate=func.now()` column expired, not refreshed

- **Rule:** After mutating a row in place (an UPDATE, not an INSERT) and
  flushing, `await db.refresh(row)` before returning/serializing it if the
  response reads a column with `onupdate=func.now()` and no client-side
  default.
- **Why:** SQLAlchemy fetches a fresh `onupdate` value via `RETURNING` on an
  INSERT but leaves it expired after a plain UPDATE; reading it outside the
  session's async context then raises `MissingGreenlet` —
  `notifications.service.archive_template` hit this serializing `updated_at`,
  even though the sibling classifier-archive path (a bare `flush()`, no
  read-back) never needed one.
- **How to apply:** Whenever a service both mutates a row's `onupdate` column
  AND returns/serializes that same row in the same call, add `refresh()`
  after the `flush()` — don't assume an existing archive-path precedent
  covers it.

## A JSONB column fed by the stock `json.dumps` rejects `Decimal` and `date`

- **Rule:** Coerce anything that is not a JSON primitive to `str` BEFORE it reaches
  a JSONB bind — the engine configures no `json_serializer`, so there is no
  encoder to fall back on.
- **Why:** `notifications.params` is stored raw, and the seeded templates ask for
  `{amount}` (a `Decimal` — money is `numeric` by project convention) and
  `{due_date}`/`{valid_from}` (`date`). The natural 3.10 call would raise
  `TypeError: Object of type Decimal is not JSON serializable` at flush, INSIDE
  the caller's business transaction — turning invoice issuance into a 500, the
  one outcome ruling 10 exists to prevent (3.5 final review; `service._jsonable`).
- **How to apply:** Any new JSONB column written from domain values gets the same
  coercion at its single write point, plus a test with a `Decimal` and a `date`.

## `secrets.compare_digest` raises `TypeError` on non-ASCII strings

- **Rule:** Compare secrets as BYTES — `compare_digest(a.encode(), b.encode())` —
  whenever either side can come from a URL path, a header or a query string.
- **Why:** `POST /api/v1/webhooks/eskiz/%CE%A9` hit the blanket 500 handler instead
  of the intended 404, on the one route whose stated invariant is that it never
  500s on garbage (3.5 final review). The str form only accepts ASCII operands.
- **How to apply:** Every future provider webhook (Payme at 3.10, my.gov.uz later)
  compares bytes, and gets a non-ASCII-path test alongside its wrong-secret test.

## Never ask a provider for a callback you cannot correlate

- **Rule:** Only request a delivery report / webhook for a send that has a stored
  row to correlate it against; pass an explicit "no callback" flag otherwise.
- **Why:** `EskizSmsSender` put `callback_url` in every payload while `RealOtpSender`
  passed a throwaway uuid as the reference, so at `sms_mode=real` EVERY OTP would
  have produced one `inbound_dead_letters` row (holding the recipient's phone
  number) plus one `integration_log` row, forever — no purge job covers dead
  letters, and the DLQ's triage purpose would drown in the noise (3.5 final review).
- **How to apply:** When wiring a provider callback, ask what the DLQ does with a
  report that matches nothing — and remove the cause rather than filtering it.

## An anonymous endpoint must cap what it PERSISTS, not just what it answers

- **Rule:** Any column an unauthenticated caller can fill gets an explicit size cap
  with a truncation marker, even where a sibling field is already truncated.
- **Why:** `inbound_dead_letters.payload` stored an arbitrary-size body from the
  anonymous Eskiz callback while `error` right beside it was cut to 1000 chars;
  there is no body-size middleware in the app and no purge job for dead letters
  (3.5 final review — `service.DEAD_LETTER_PAYLOAD_MAX_BYTES`). "Every other JSON
  endpoint does the same" was the wrong defence: the others do not PERSIST the body.
- **How to apply:** New anonymous ingest path → cap what it writes, and say in the
  stored row that it was capped.

## `sa.literal(value, JSONB)` inside `.bindparams()` binds the wrong object

- **Rule:** To insert a JSON/JSONB literal from a raw migration `sa.text(...).bindparams(...)`
  call, pre-serialize with `json.dumps` and bind it as plain text with an explicit
  `CAST(:x AS jsonb)` in the SQL — never pass `sa.literal(value, postgresql.JSONB)`
  as the keyword value.
- **Why:** `.bindparams(key=sa.literal(v, type_))` sets the param's bound value to
  the `BindParameter` construct itself, not `v` — asyncpg then receives that
  construct where it expects serialized text and raises `DataError: ... object has
  no attribute 'encode'` (migration 0010, caught by the RED/GREEN cycle, never
  reached a running database).
- **How to apply:** A raw-SQL JSONB insert in a migration goes through `op.bulk_insert`
  with a `sa.table`/`sa.column(..., postgresql.JSONB())` and a plain Python dict
  (0009's pattern, unaffected by this bug) whenever it fits, or `json.dumps(value)`
  bound as text plus `CAST(:x AS jsonb)` otherwise — never a JSONB-typed `sa.literal`
  inside `bindparams`.

## A fixed-scale `NUMERIC` column round-trips at its own precision, not the caller's

- **Rule:** A pydantic response field backed by a `NUMERIC(p,s)` column strips
  trailing zeros explicitly (`format(value, "f").rstrip("0").rstrip(".")`) before
  the API returns it — never assume the value keeps the request's own precision,
  and never reach for `Decimal.normalize()` to do the stripping.
- **Why:** `contour_versions.declared_area_ha` is `NUMERIC(12,4)`; posting `"2.6"`
  and reading it back gives `Decimal('2.6000')` (confirmed against real Postgres,
  not just SQLAlchemy) — a bare `Decimal` field then serializes as `"2.6000"`,
  silently failing a test asserting the reference figure `"2.6"` (task 3). Fixing
  it with `.normalize()` trades one bug for another: `Decimal('100.0000')
  .normalize()` is `Decimal('1E+2')`, not `100` — wrong for a whole-number area.
- **How to apply:** Any new `Decimal`-backed response field on a fixed-scale
  column gets a `field_serializer` doing the `format(..., "f")`-then-`rstrip`
  dance (`gis.schemas._trim_decimal`), and a test asserting the exact JSON string
  a round-tripped value produces — not just its `float()`.

## A `_client_for`-style fixture's setup-time commit only covers what ran before it

- **Rule:** When a test combines a signed-in HTTP-client fixture that commits
  internally (e.g. `gis_client`) with a SEPARATE fixture writing through the same
  `db` session (e.g. `leshoz`), the write-fixture's row is invisible to the app's
  own session unless something commits it again AFTER that fixture runs — pytest
  instantiates a test's fixtures in the LEFT-TO-RIGHT order of its parameter list
  (verified empirically with a throwaway fixture-order test), so anything listed
  AFTER the client is not covered by the client's own setup-time commit.
- **Why:** `test_gis_specialist_creates_a_contour_and_a_draft_version(gis_client,
  leshoz, contours_layer)` would FK-fail otherwise: `gis_client`'s internal commit
  runs before `leshoz` even executes, so `leshoz`'s `flush()`-only row stays
  invisible to the app's separate connection — confirmed empirically with a fresh,
  independent asyncpg connection reading `organizations` right after fixture setup
  and finding nothing there (task 3, first task to combine a `_client_for` client
  with a write fixture in the same test).
- **How to apply:** Every client fixture built over `_client_for`
  (`tests/modules/gis/conftest.py`) registers an httpx `request` event hook
  (`_commit_pending_before_requests`) that re-commits `db` right before every
  outgoing call, so any other fixture's writes are picked up regardless of listed
  order. Copy this pattern for any new signed-in-client fixture — in gis or
  another module — that will ever be combined with a write fixture in the same
  test; do not assume the client fixture's own setup-time commit is enough.

## `DomainError`'s JSON response has no encoder — a raw Decimal/UUID in `details` is a 500

- **Rule:** Before passing any value read from the database (not a hand-built
  dict of plain strings) into `err(..., details=...)`, convert it to a
  JSON-safe structure first (`float`/`str`, recursively) — never assume
  `details` gets the same treatment a pydantic `response_model` would.
- **Why:** `app.main`'s `DomainError` handler renders the response with
  Starlette's `JSONResponse` — stock `json.dumps`, no encoder configured at
  all — unlike a `response_model` route, which goes through pydantic's own
  serializer. `gis.service.publish_version`'s `ERR-GIS-003` details carry
  `checks._intersections`' raw `Decimal` (`area_m2`) and `uuid.UUID`
  (`feature_id`): passing them through unconverted raised `TypeError` INSIDE
  the exception handler itself while it built the response, turning a
  blocked publish's clean 422 into a 500 (stage 3.6a task 5; caught by the
  task's own overlap test, confirmed by temporarily reverting the fix and
  watching the same test fail with exactly that traceback).
- **How to apply:** Any new `err(..., details=...)` call whose details
  originate from a DB read needs its own recursive JSON-safety pass first —
  `gis.checks.jsonable` is the template AND the one place to extend it. It
  started as two near-identical local copies (`service._checks_jsonable`,
  `schemas._jsonable_details`, one per consumer) and they had ALREADY
  diverged by the time the task's own review caught it — the schemas copy
  had no `uuid.UUID` branch, silently relying on pydantic's own Any-typed
  encoder to cover for it on that one path only. Unlike `_json_safe`
  (deliberately kept as separate, DIFFERENTLY-behaved local copies per
  consumer — see its own entry above), a coercer whose two callers need
  IDENTICAL conversions belongs in one shared function, not a mirror: a
  mirror only earns its keep when the two copies are supposed to diverge.

## A fixed test geometry that a `_client_for` client commits accumulates forever

- **Rule:** A fixture whose test needs an EMPTY neighbourhood (a "nothing else
  overlaps here" assertion, for any geometry-bearing table) picks a randomised
  location — `tests/modules/gis/conftest.py::random_box_wkt()` — never a fixed
  literal, even a currently-unused-looking one.
- **Why:** `published_contour` + a `gis_client`-family fixture commits for real
  (the previous lesson's request hook), so every past run of
  `test_a_draft_version_can_be_edited_but_a_published_one_cannot` (and any test
  like it) has left another published contour at box_wkt(69.9, 41.5) in the
  shared, persistent test DB — confirmed empirically while building task 4's
  `overlap` check: 4 leftover rows there before this task's own runs, 11 after a
  handful more. A NEW test asserting "no overlap" at that same coordinate is
  flaky by construction from the moment it's written, not from bad luck later.
- **How to apply:** Grep `tests/modules/gis/` for `box_wkt(69.9, 41.5)` (and its
  69.91/69.905/60.0 neighbours) before adding a new fixture near it; if the test
  needs isolation rather than deliberate proximity to an existing fixture
  (`neighbouring_published_contour` and its siblings need the shared literal,
  by design — they're robust to extra copies at that spot, verified), use
  `random_box_wkt()` instead. Applies beyond gis to any future table that
  stores real geometry and gets exercised through a committing client fixture.

## A seeded notification template becomes undeletable once something has sent it

- **Rule:** A migration whose `downgrade()` deletes a seeded `notification_templates`
  row must delete the `notifications` referencing it FIRST — and any migration that
  seeds a template owes its downgrade both statements from the start.
- **Why:** 0010 seeded `gis.import.finished` and deleted only the template on the way
  down. That was fine for two tasks, because nothing sent the event yet; the moment
  task 7's import job actually sent it, `fk_notifications_template_id_notification_templates`
  turned the CI downgrade→upgrade round-trip red — in a task that never touched the
  migration — and a real rollback of 0010 would have failed the same way in production.
- **How to apply:** Seeding a template in a migration means writing
  `DELETE FROM notifications WHERE event_code = '<code>'` immediately above the
  template delete, not when someone finally sends it; and when the round-trip test
  goes red in a task that changed no migration, look for the event that task started
  emitting.

## A queue-wide `FOR UPDATE SKIP LOCKED` claim makes a whole test module order-dependent

- **Rule:** A worker that claims the OLDEST row of a table needs a drain step in an
  autouse fixture at the PACKAGE conftest level, not in one test module — and the
  drain runs the job itself, never an unscoped `DELETE`.
- **Why:** `gis.import_service.process_pending` claims the oldest `pending` import in
  the database, not the one the test created, and every import fixture commits (the
  job runs in a session of its own and cannot see an uncommitted row). An interrupted
  run therefore strands a `pending` row forever in the shared, persistent test DB, and
  the next run claims that stranger instead of its own: `test_two_workers…` sees
  `[1, 1]` instead of `[0, 1]`. A module-local drain only masked it, and only because
  that module sorted first and happened to drain the other module's leftovers.
- **How to apply:** Any future claim-the-oldest worker (batch publication in 3.6b,
  report generation, export jobs) gets `tests/…/conftest.py`'s
  `drain_pending_imports` shape: autouse, package-scoped, bounded by a DRAIN_LIMIT
  that fails loudly rather than looping. Fixed test literals in the same fixtures
  (a `storage_key`, a contour number) need randomising for the same reason.

## A per-endpoint guard is not a root fix when sibling endpoints share a precondition but gate on different permissions

- **Rule:** When several endpoints each perform one step of a shared multi-step
  transition (submit-review/approve/publish), a precondition belonging to the
  WHOLE transition — "is this even a valid target for this workflow at all" —
  must live in ONE function every step calls, never only in the step a
  well-behaved caller happens to reach first.
- **Why:** `gis.service.submit_import_review` alone got the "refuse a
  non-contour import batch" guard in task 8's first review fix;
  `approve_import`/`publish_import` still gated on `row.status` alone. Since
  `CONTOURS_APPROVE` (the rahbar's own permission) is a DIFFERENT permission
  from `CONTOURS_MANAGE` (submit-review's), a rahbar-only actor could call
  `/approve` directly on a batch that had just finished parsing — never
  having called, or being able to call, submit-review at all — and both loops
  silently found zero `ContourVersion` rows and still advanced
  `gis_imports.status`, reaching the exact false "done" with zero published
  the guard was written to prevent. The contour-batch sibling of the same bug:
  `/approve` called before `/submit-review` finds zero `review`-status
  versions and still sets `row.status = "approved"`, having approved nothing.
  Reproduced for real with `git stash` on the fix (both cases answered `200`,
  not `409`) before confirming the root-cause version.
- **How to apply:** Any future multi-step, multi-permission transition (norms'
  own Draft→Review→Approved→Published cycle is the next one to carry this
  shape) factors its shared preamble — row lookup, zone check, any "is this a
  valid target" check, status check — into one function every step calls, and
  separately refuses a transition whose loop would move zero child rows: a
  loop finding nothing is not evidence that nothing needed to happen.

## The rahbar's role code is `leadership`, not `rahbar`

- **Rule:** Before granting a permission to "the raҳbar" (leshoz head) in a
  migration or a plan, check `roles.code` in migration `0003_auth` — it is
  seeded as `leadership`. There is no role code `rahbar`.
- **Why:** `plans/03.6a-gis-core.md` was written with role code `rahbar`; an
  `INSERT … SELECT … WHERE code = 'rahbar'` inserts zero rows silently, so
  `gis.contours.approve` would have reached nobody and every "the rahbar
  approves" test would have passed for the wrong reason (a personal grant,
  not the role) — caught only because the implementer ran it and watched the
  grant not land (stage 3.6a Task 1).
- **How to apply:** Any future migration or plan that seeds a role-based
  permission grant: grep `leadership`/`rahbar` in `migrations/versions/
  0003_auth.py` first, never trust a role name from spec/plan prose alone.

## `IntegrityError` IS a `DBAPIError` — the narrow `except` must come first

- **Rule:** When a function needs to catch both `IntegrityError` and the
  broader `DBAPIError`, write `except IntegrityError` BEFORE `except
  DBAPIError` — never after, and never as one clause that inspects `exc.orig`
  by hand instead.
- **Why:** `gis.service.create_version` originally had only `except
  DBAPIError`, mapping every DB failure to `ERR-GIS-001` ("unreadable
  geometry"); a `uq_contour_version_no` race from two concurrent creates
  raises `IntegrityError`, a `DBAPIError` subclass, so it was silently
  swallowed by the broad clause and reported as a geometry defect instead of
  the version-number conflict it actually was (stage 3.6a Task 3, final
  review finding 3 — confirmed empirically against real Postgres, not from
  documentation, that the two failure modes even raise different exception
  types; Task 7's bulk importer drives the very same path, so the bug was
  live, not theoretical).
- **How to apply:** Before adding a second `except` clause alongside an
  existing `except DBAPIError`, check whether the new one is a subclass
  (`IntegrityError.__mro__`) — if so, order it first, and verify empirically
  which exception a given real constraint violation actually raises rather
  than assuming from the constraint's name.

## A service-level pre-check can leave its mirrored DB CHECK permanently unexercised

- **Rule:** When a service pre-checks a rule a DB CHECK also enforces (this
  project's defense-in-depth pattern), write a SEPARATE model-level test that
  inserts through the ORM directly, bypassing the service — an API-level test
  alone never reaches the CHECK.
- **Why:** `Contour.parent_needs_subcontour` has had a test since Task 1, but
  it only ever went through `POST /gis/contours`, which raises `ERR-VAL-001`
  from the SERVICE's own pre-check before a row is even constructed — the
  CHECK itself was never fired by any test until Task 3 added one that builds
  `Contour(...)` directly and asserts the `IntegrityError` (Task 1 deferred
  minor, closed at Task 3).
- **How to apply:** Any CHECK mirrored by a service-level guard needs two
  tests, not one: the service's 422/409 through the API, AND a direct-ORM
  insert asserting the CHECK's own `IntegrityError` — when adding a
  `CheckConstraint`, grep for whether a `pytest.raises(IntegrityError)` test
  actually exercises it, not just the guard in front of it.

## An empty layer makes a containment check meaningless — decide what "no data" means before the data exists

- **Rule:** Before a topology/containment check goes live against a layer or
  table that might still be empty, implement its THIRD outcome — `skipped`,
  never `pass` or `fail` — for the "no reference data yet" case, decided at
  design time rather than left for whoever notices the check always fires the
  same way.
- **Why:** `gis.checks._within_fund` (`ST_Within` against the `forest_fund`
  layer) would report every contour as `outside_forest_fund` — a hard `fail`
  blocking every publication — for as long as the Agency's fund-boundary
  delivery is pending (plan ruling 9); without the `skipped`/`layer_empty`
  branch (returned straight from `count(*) == 0` in the same query that would
  otherwise test containment), not one contour could have published this
  month. The check turns itself on the day the data lands, with no code
  change.
- **How to apply:** Any check whose candidate/reference set is a layer or
  table this project does not yet fully control the population of (a layer
  awaiting real Agency data, a not-yet-onboarded integration) gets an
  explicit empty-set branch decided up front, returned as its own named
  result — never silently defaulted to `pass` or `fail` once real rows start
  arriving.

## `ST_Intersects` alone reports every shared border as an overlap — real cadastral data needs an area tolerance

- **Rule:** A geometric "do these overlap" predicate over real (hand-digitised
  or GIS-sourced) polygons is never plain `ST_Intersects`/`ST_Overlaps` —
  compute the actual intersection area and compare it against a configurable
  tolerance, because two legitimately adjacent polygons share a border of
  zero area, and `ST_Intersects` is `true` for that exactly as it is for a
  genuine double-booked overlap.
- **Why:** Two neighbouring published contours sharing a fence line are the
  NORMAL case on real cadastral data, not an edge case — a plain-`ST_Intersects`
  publish-blocking check (the spec's own `ST_Overlaps`) would have refused to
  publish perfectly valid neighbouring contours (decision #24's correction;
  `gis.checks._intersections` computes `ST_Area(ST_Intersection(a,b)
  ::geography)` and compares it to `gis_overlap_tolerance_m2`, default
  100 m², proven by `test_checks.py`'s `draft_version_touching_it` vs
  `draft_version_overlapping_it` pair — one shares an edge and must pass, the
  other genuinely overlaps and must block).
- **How to apply:** Any new geometric predicate over contour/parcel-shaped
  data (norms' own territory checks, 3.7+, are the next candidate) computes
  an intersection AREA and compares it to a named, configurable tolerance
  setting — never a bare boolean `ST_Intersects`/`ST_Overlaps` used directly
  as the blocking test.

## `request.body()` raises inside a dependency on any FORM route

- **Rule:** A FastAPI dependency that needs the raw request body (`auth.deps.
  idempotency_context` is the only one) must branch on the content type: for
  `multipart/form-data` and `application/x-www-form-urlencoded` it reads
  `await request.form()` (Starlette's cached `FormData`), never
  `await request.body()`.
- **Why:** FastAPI reads the body BEFORE solving dependencies, and for a form it
  parses straight off the stream rather than caching bytes — so `Request.body()`
  re-enters `Request.stream()`, hits `_stream_consumed` and raises
  `RuntimeError("Stream consumed")`, an unhandled 500. Hit the moment
  `POST /gis/imports` became the idempotency mechanism's first consumer (3.6a
  fix wave); reproduced by wiring the dependency the plain way and watching the
  upload come back 500.
- **How to apply:** Any future `Idempotency-Key` consumer that takes a file
  (payment receipts, act scans) is a multipart route and inherits this; the
  fingerprint there is the form's scalar fields plus each file's
  name/filename/content-type/size, sorted — never the file bytes, which would
  mean re-reading a 100 MB upload per request. **The mirror is a trap too:** the
  branch keys on the CONTENT TYPE, not on the route, so a JSON-body route
  declaring `idempotency_context` that is sent `multipart/form-data` takes the
  form branch, `request.form()` consumes the stream, and FastAPI's OWN
  `await request.body()` for the JSON body field then raises the same
  `RuntimeError`. Not reachable today — `imports_router` is the only consumer
  and is itself multipart — but the next consumer needs to know, and a route
  that accepts both shapes needs the branch reconsidered rather than reused.

## A status-transition table is ambiguous wherever two source states share a target

- **Rule:** When a state machine allows one target from more than one source
  (`TRANSITIONS`: `review` is reachable from BOTH `draft` and `approved`),
  EVERY route driving any of those edges must assert the SOURCE state too, not
  only "may this row become X" — the new route and the one that was already
  there.
- **Why:** `return-to-review` (`CONTOURS_APPROVE`) and `submit-review`
  (`CONTOURS_MANAGE`) both land on `review`. Checking the target alone let the
  approver drive `draft` -> `review` — submit-review's own edge, under the wrong
  permission — so an approver could advance a specialist's draft they otherwise
  may not touch. Caught by the new route's own bad-transition test, which
  returned 200 instead of 409 (3.6a fix wave). The MIRROR was still open after
  that fix and had to be closed separately: `submit_review` (`CONTOURS_MANAGE`)
  kept the bare check, so it drove `approved` -> `review` — the rework edge just
  gated behind `CONTOURS_APPROVE` — and audited it as `submit_review`. Guarding
  only the route you are adding leaves the split it installs defeated from the
  other side.
- **How to apply:** Before adding a route to an existing transition table, grep
  the table for the target: more than one source means EVERY route reaching
  that target needs `_assert_transition_from(version, source, target)`, not
  just the new one. Fix the siblings in the same commit.

## Paging a list breaks every test that asserted membership in the unpaged one

- **Rule:** When you add `Page[T]` to a list endpoint, every existing test that
  asserted "my fixture's row is in the response" must gain a filter narrowing to
  that fixture's own data — a fresh `organization_id` is the usual one here.
- **Why:** The test DB is shared and persistent, and committing client fixtures
  leave their rows behind forever, so page 1 of 20 is full of previous runs'
  contours: `test_an_applicant_sees_published_contours_only` went red the moment
  `GET /gis/contours` was paged, for a reason that had nothing to do with the
  change (3.6a fix wave).
- **How to apply:** Page an endpoint and grep its tests for unfiltered `GET`s in
  the same commit; assert against `total` and an explicitly scoped query, never
  against membership in an unbounded default page.

## A re-exported fixture shadowed by a same-file parameter trips ruff's F811, unlike a fixture defined locally

- **Rule:** When a conftest.py imports another module's fixture ONLY to re-export
  it (`# noqa: F401`) and ALSO uses that same name as a plain parameter on a
  fixture defined in the SAME file, import it as `from module import name as
  name` instead — never rename the parameter, since that would break pytest's
  name-based fixture injection.
- **Why:** Pyflakes flags a parameter shadowing an import it considers "unused"
  as `RedefinedWhileUnused` (F811), even though the identical shape is silent
  when the earlier binding is a locally-defined `@pytest.fixture` function
  instead of an import — confirmed empirically both ways: `tests/modules/gis/
  conftest.py`'s own `published_contour(db, contours_layer, leshoz,
  approval_doc)` never trips it (those four are DEFINED there), while
  `tests/modules/norms/conftest.py` re-exporting the same four from gis's
  conftest and then using them as parameters on `published_contour`/
  `draft_only_contour`/three client fixtures failed `ruff check` on all four
  until switched to `as`-imports (stage 3.7 task 1; verified in isolation with
  a two-line repro — `import os  # noqa: F401` then `def foo(os): return os`
  — under this project's exact ruff config).
- **How to apply:** Any new conftest.py that re-exports another module's
  fixture AND ALSO consumes it locally by parameter name: keep `# noqa: F401`
  for names you only re-export, but import the ones you also use as a local
  parameter with `as <same name>` — `ruff check --fix` will even relocate each
  into its own `from ... import (...)` statement; let it.

## The round-trip test's expected head version is a hardcoded string every new migration must bump

- **Rule:** After adding a migration, update `tests/test_migrations.py::
  test_downgrade_upgrade_roundtrip`'s `assert version == "<old head>"` to the
  new revision id, in the same commit.
- **Why:** The assertion is a literal string, not derived from `alembic
  heads` — a brand-new migration passes every test of its OWN and still fails
  this one with a confusing `assert '0011' == '0010'` that reads like a
  migration-chain bug rather than a one-line test update (hit adding 0011 in
  stage 3.7 task 1; migration 0010 must have needed the identical bump from
  `"0009"` and left no trace of having done so).
- **How to apply:** Whenever a new migration advances the head, grep
  `tests/test_migrations.py` for the previous head string and update it as
  part of the same commit — it is not on any task brief's file list by
  default, so it is easy to only discover by actually running the suite.
