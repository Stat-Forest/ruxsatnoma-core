# Ruxsatnoma Lessons

Hard-won gotchas specific to this backend. **Read this file before any coding work.**
A lesson beats an instinct; a `../docs/decisions.md` ruling beats a lesson.

What does NOT belong here: general Python/FastAPI advice, a project convention
(`CLAUDE.md`), a deploy-only fact (`CLAUDE.md` → Deploy notes), a product ruling
(`../docs/decisions.md`), an open question (`../docs/tz/12-otkrytye-voprosy.md`).

**Adding a lesson: use the `writing-lessons` skill** (`.claude/skills/writing-lessons/`).
It carries the three steps missing from "APPEND a lesson", which is how this file reached
1217 lines: can it be a mechanical check instead, does the class already have an entry,
and is it a lesson at all rather than a convention or a ruling.

Format and budget:

```
## {Short topic title — state the rule, not the symptom}
- **Rule:** {1–2 lines: what to do}
- **Why:** {2–3 lines: the incident with its concrete symptom — error, status code, file}
- **How to apply:** {1–2 lines: the trigger, and where the reference implementation lives}
```

**A new entry is at most 12 lines.** Only a MERGE — an entry replacing two or more existing
ones — may go past that, and never past 24. Over 12 while deleting nothing means you are
writing a story: the incident is evidence, so name the error and the file, and drop the
retelling of how it was found.

**The file itself is capped at 60 entries / 1000 lines.** Short entries still add up, and
reading cost is how many times how long. Once the cap is reached, a lesson is paid for by
merging two old ones of the same class, or deleting one whose trap a check has since made
impossible — the point, not an obstacle. A pre-commit hook, `make check` and CI enforce
both limits; `make lessons-check` runs the same check alone.

Rules for this file:

- **One class, one entry.** Before appending, grep the headings below for the class.
  If an entry covers it, sharpen that one and add your case as a sub-bullet.
- Only what is NOT obvious from reading the code. "Use async" is a convention;
  "`\d` means something different in Postgres" is a lesson.
- Every entry states a real trigger. No hypotheticals.
- *(from ControlAI)* marks an entry carried over from a sibling project on a close
  stack, each verified to apply here before it was written down.

Sections: Migrations and schema · DB constraints vs Python · Values: dates, decimals,
precision · JSON boundaries · Permissions, roles, transitions · Root fixes ·
PostGIS · HTTP layer · Outbox and integrations · Tests and the shared test DB ·
Tooling and environment.

---

# Migrations and schema

## A model file missing from `models_registry.py` yields an EMPTY migration, silently

- **Rule:** After adding `app/modules/<name>/models.py`, import it in
  `app/models_registry.py` in the same commit, then read the generated migration
  before applying it.
- **Why:** Autogenerate only sees what `Base.metadata` knows about. A forgotten import
  produces a valid, empty, green migration — no error, no warning. Open since 3.1; the
  sweep autotest is still not written (`tests/conftest.py` only imports the registry).
- **How to apply:** New model file → registry import → `alembic revision --autogenerate`
  → the diff must be non-empty and must contain your tables.

## A downgrade must delete whatever its upgrade made possible — and an append-only referrer blocks even the nulling UPDATE

- **Rule:** A migration that widens a CHECK, or seeds a row other tables reference, owes
  its `downgrade()` the matching `DELETE`, written with the migration. An append-only FK
  table's own trigger blocks even the nulling `UPDATE` a plain `DELETE` needs first to
  clear the reference.
- **Why:** Three shapes. `0006` widened `otp_codes.purpose` past rows already violating the
  narrower CHECK; `0010` seeded `gis.import.finished`, deleted only the template, and broke
  `fk_notifications_template_id_notification_templates`; `0023` (3.11b) seeds
  `permit_status_reasons`, which `permit_status_history.reason_item_id` FKs to — a bare
  `DELETE` hits that FK, and nulling it first with a bare `UPDATE` hits the table's OWN
  append-only trigger, invisible against an EMPTY database, real once one real decision is
  signed.
  - **A fourth shape, `0045` (7.9 task 4):** `payment_recipients`' seeded budget row stayed
    unreferenced until `issue_invoice` wrote a real COMMITTED `invoice_recipients` row,
    then the seed's `DELETE` hit that FK — fixed by dropping the `DELETE`, since
    `invoice_recipients` is itself dropped later in the SAME `downgrade()`.
- **How to apply:** Widening a constraint → data-cleanup statement in the downgrade.
  Seeding a template → delete the notification rows above the template delete. Seeding a
  row an APPEND-ONLY column will FK to → wrap the nulling `UPDATE` in `DISABLE`/`ENABLE
  TRIGGER USER` (never `ALL`), then the `DELETE`. A referencing TABLE dropped later in the
  SAME downgrade goes before the parent delete instead.

## A new Alembic head needs both a merge migration and the round-trip test's literal moved

- **Rule:** Two branches, two migrations → `alembic merge heads`, never a hand-edited
  `down_revision`; same commit, bump `test_migrations.py`'s hardcoded head-literal assertion.
- **Why:** Hand-editing history breaks every environment on the original revisions
  (*ControlAI*); the literal isn't derived from `alembic heads`, so a new migration fails it
  alone, reading like a chain bug (0011, 3.7 t1; `merge_0018_0020`, 3.9b/3.11b).
- **How to apply:** `alembic merge heads -m "merge"`; `make heads` is the gate — grep the
  test for the previous head string, since no task brief lists that file.
- **Three confirmed mirrors, all from re-pointing `down_revision` by hand instead of
  merging:** 3.11b (2026-09-07) re-pointed `0023`'s parent `0022`→`0025`; a database
  already past `0023` never runs `0025` (forward-only) — symptom `alembic_version = 0035`
  with `application_checks.created_by` missing, 500 on submit. 7.7 (2026-09-08) hit the
  identical shape re-pointing `0038`-`0042` (`0039`→`0042`, `0040`→`0038`): a DB stamped
  `0042` would skip `0038` — `admin/repo.py::list_activity_types` would 500 the catalog,
  wizard and price calculator. Both fixed with a merge revision instead, verified from both
  heads on a scratch DB first. **A recorded revision does not prove a schema** — only a
  `base`-up roundtrip sees a splice.
- **The one exception, 2026-09-07 — same id, not a splice:** stage 5.1 and 7.6 each minted
  `0041` off `0040` five minutes apart; both CI runs passed, so `dev` got `heads: 0041,
  0041`, which `alembic merge heads` cannot merge (identical ids). The second is RENUMBERED
  and re-pointed at the first — the one case where hand-editing `down_revision` is correct,
  since nothing has run it anywhere yet. The only real guard against a repeat is
  repository-side ("branch up to date before merging"), never a local hook.

## The PostGIS image installs extensions Alembic will then want to drop

- **Rule:** A fresh `postgis/postgis:16-3.4` volume ships `tiger_geocoder`, `topology` and
  `fuzzystrmatch`; the init script `docker/…/02-drop-image-extras.sql` removes them — keep it.
- **Why:** Those extensions bring their own tables, so `alembic check` on a raw dev DB
  reports a diff unrelated to our models and autogenerate proposes dropping them (`8c00270`).
- **How to apply:** If `alembic check` is suddenly dirty on a machine that just recreated
  its volumes, check for those schemas before suspecting the models.

## A raw `sa.text()` query has sharp edges neither asyncpg nor pyright will catch early

- **Rule:** Inside a migration's `sa.text(...)`, always write `CAST(:name AS type)` — never
  `:name::type`, and never `.bindparams(name=sa.literal(v, sometype))` for the value itself.
  Build a dict from its `Result` with a comprehension — `{row[0]: row[1] for row in
  rows.all()}` — never `dict(rows.all())`.
- **Why:** three traps, one boundary. `:name::type` — `TextClause`'s regex backtracks its
  trailing lookahead, registering the param as `valu` not `value`, so
  `.bindparams(value=...)` raises `ArgumentError: ... doesn't define a bound parameter
  named 'value'` (migration 0012, 3.7 t2). `sa.literal(v, postgresql.JSONB)` as a bind
  VALUE binds the `BindParameter` construct itself, not `v` — asyncpg raises `DataError:
  ... object has no attribute 'encode'` (migration 0010). And that query's rows are
  `Row[Any]`, so `dict(rows.all())` matches the wrong `dict()` overload and pyright reports
  `reportCallIssue` on code that runs correctly (migration 0012 tests, 3.7 t2) — a TYPED
  `select(Col.a, Col.b)` does not trip it.
- **How to apply:** Grep any new raw-SQL migration or test for `:\w+::` before running it.
  For a JSONB literal, prefer `op.bulk_insert` with `sa.column(..., postgresql.JSONB())`
  and a plain dict (0009's pattern), or pre-serialize with `json.dumps` and bind as text
  under `CAST(:x AS jsonb)`. Reserve `dict(rows.all())` for a typed `select(...)`.

## A bare `alembic` CLI command targets the shared dev DB, not your worktree's test DB

- **Rule:** Never run `uv run alembic revision --autogenerate` or `upgrade head` bare in a
  worktree — export `DATABASE_URL="$DATABASE_URL_TEST"` for that one command first (mirror
  `tests/conftest.py::_migrated_test_db`).
- **Why:** `migrations/env.py::_resolve_url()` falls back to the shared dev DB whenever
  nothing overrides it, and a sibling worktree's not-yet-merged branch may have already
  advanced ITS `alembic_version` past revisions yours doesn't have — the bare command then
  fails `Can't locate revision identified by '0013'`, reading like local corruption rather
  than a shared DB ahead of your files (hit building 3.8).
- **How to apply:** Before any manual Alembic CLI use, set the override. A revision error
  naming a version you don't have locally means check which DB you connected to first.
- **Recovery:** Already ran it bare? Check `alembic_version` on the dev DB immediately; if
  it names one of YOUR unmerged revisions, `alembic downgrade` back to where `dev` actually
  is before touching anything else (recovered clean in 3.10a t6, back to `0014`).

## Amending an unmerged migration needs the OLD script to downgrade, the NEW one to upgrade

- **Rule:** Editing an unmerged migration's `upgrade()`/`downgrade()` after your OWN
  worktree already ran it: `git show HEAD:<file> > <file>` to restore the OLD content,
  `alembic downgrade -1`, THEN restore your edit and `alembic upgrade head`.
- **Why:** The installed schema still matches the OLD script; running the EDITED file's
  `downgrade()` against it fails on whatever the edit added —
  `UndefinedObjectError: index "ix_invoices_calculation_id" does not exist`, amending 0017
  to add a column after `_migrated_test_db` had already created the table without it (3.10a t2).
- **How to apply:** Any "amend this branch's own migration in place" ruling, on a worktree
  whose test DB already applied it — downgrade with the git-HEAD version, never the edited one.

## Closing a deferred FK can break a DIFFERENT module's tests, invisibly

- **Rule:** After a migration adds `NOT VALID` + `VALIDATE CONSTRAINT` on a column another
  module already writes to, `grep -rn '<column>' tests/` across the WHOLE suite before
  reporting done — not just the tests your own file list names.
- **Why:** Migration 0015 closed `calculations.application_id`'s FK, absent since 3.7
  because `applications` didn't exist yet; `test_calculations_api.py` inserted
  `Calculation(application_id=uuid.uuid4())` directly — a fabricated id, valid only because
  no table existed — and started raising `ForeignKeyViolationError`. `make heads` and the
  autogenerate-diff test saw nothing; only a full `pytest -q` surfaced it (3.9a t1).
- **How to apply:** Closing any deferred FK (grep migration history for `NOT VALID` to find
  the others), grep the column name suite-wide first, then run `make check` in full.

---

# DB constraints vs Python

## `\d` is Unicode-aware in Python but ASCII-only in a Postgres CHECK

- **Rule:** In any pattern guarding a column that a DB CHECK also constrains (`pinfl`,
  `stir`, phone, codes), write the ASCII class explicitly — `^[0-9]{9}$`, never `^\d{9}$`.
- **Why:** pydantic/Python `\d` matches Arabic-Indic and other Unicode digits, the CHECK
  does not — a non-ASCII digit string passes validation and 500s at the DB. Hit twice:
  `organizations.stir` (`527d4e0`) and `users.pinfl` (3.3b).
- **How to apply:** A schema pattern and a CHECK describing the same field must be the SAME
  regex dialect — read the migration before writing the pydantic pattern.

## A partial unique index constrains only the rows it covers, and only after a flush

- **Rule:** Re-read the index's `WHERE` clause and ask what happens OUTSIDE it; and in a
  supersede write, `flush()` between the archive UPDATE and the new INSERT.
- **Why:** Two faces of one index. The classifier uniqueness index covers `status='active'`
  only, so superseding an already-archived item inserted a second overlapping row and a
  historical `on_date` lookup returned two rows for one code (3.3a). And without the flush,
  UPDATE and INSERT are both still pending when the index is checked, so the insert raises
  `IntegrityError` on a conflict the flush would have resolved.
- **How to apply:** Rows a partial index does not cover need a service-layer guard. Every
  supersede write (classifier items 3.3a, notification templates 3.5) is
  `old.status = "archived"` → `await db.flush()` → `db.add(new_row)`, in that order.

## An enum-ish column has ONE source of truth: the tuple

- **Rule:** Define the allowed values as a module-level tuple, build the DB CHECK from it
  (`CheckConstraint(f"col IN {TUPLE}")`), spell the pydantic `Literal` members out by hand,
  and close the gap with `assert set(get_args(TheLiteral)) == set(THE_TUPLE)`.
- **Why:** Retyping the literals is two sources of truth — a value added on one side is a
  422 that should be a 201, or an `IntegrityError` 500 that should be a 422. But
  `Literal[*TUPLE]` is not the fix: it runs and pydantic accepts it, while pyright reports
  `reportInvalidTypeForm` ("Variable not allowed in type expression") — a `Literal`'s members
  must be statically visible (3.7 fix wave, finding I8).
- **How to apply:** `norms/models.py`'s `LIVESTOCK_GROUPS`/`QUANTITY_UNITS` +
  `norms/schemas.py`'s `LivestockGroup`/`QuantityUnit` +
  `test_models.py::test_the_schema_literals_match_the_tables_own_check_constraints` are the
  shape. `admin.models.ORGANIZATION_KINDS` still retypes its CHECK — fix on next touch.

## Two mechanisms refusing one thing: an outcome-only test cannot tell which one fired

- **Rule:** When something else also refuses what your guard refuses — a DB CHECK behind a
  service pre-check, a later guard in the same function — assert the RECORDED reason, not
  just the status code, and revert the guard to prove that test goes red.
- **Why:** Twice, one class. `parent_needs_subcontour`'s only test went through
  `POST /gis/contours`, which raises `ERR-VAL-001` before a row exists, so the CHECK never
  ran (3.6a t3). `permits.signers.is_known` was redundant — an unmapped purpose fell
  through to the role comparison as `wrong_role`, so deleting the guard left it green.
- **How to apply:** Adding a guard in front of an existing refusal → one test per
  mechanism, each asserting its OWN reason (a direct-ORM `pytest.raises` for a CHECK). A
  negative control that stays green means the guard is redundant, not that it works.

## A failed DB statement aborts the whole transaction — catch the right type, recover with a SAVEPOINT

- **Rule:** `except IntegrityError` before `except DBAPIError`, never after; name the
  constraint via `getattr(exc.orig.__cause__, "constraint_name", None)`, never `exc.orig`.
  Keep writing on the same session after any failure inside `async with db.begin_nested():`
  (a SAVEPOINT), never a bare `db.rollback()`.
- **Why:** `gis.service.create_version` caught only `DBAPIError`, so a
  `uq_contour_version_no` race (`IntegrityError`, a subclass) was reported as `ERR-GIS-001`
  "unreadable geometry" (3.6a t3). Never judge from the name: an append-only trigger's
  `RAISE EXCEPTION` is SQLSTATE `P0001`, outside `23xxx`, and also surfaces as `DBAPIError`
  — as does `DeadlockDetected`, missed by `except DomainError`.
- **The recovery half:** a bare `db.rollback()` undoes the WHOLE transaction, not just the
  failed statement — `sign()`'s race path silently discarded a caller's earlier uncommitted
  work. Worse in a loop: Postgres then refuses every later statement and turns the final
  `COMMIT` into a silent `ROLLBACK` — one savepoint PER ROW keeps the next row writable.
- **The savepoint's own trap:** its ROLLBACK expires every instance dirty inside it, so
  reading `row.applicant_id` in the `except` is a lazy reload — `MissingGreenlet` from
  inside the handler, a 500 where the clean 409 was (3.9a t5). Copy what the handler needs
  into locals BEFORE the `async with`.
- **Your handler is not the end of the transaction:** `get_db` commits AGAIN after it
  returns, so a route swallowing a DB failure and answering anyway (`payme_router.py`,
  always-200) must `await db.rollback()` first, or that second commit 500s.
- **How to apply:** `signatures.service.sign()` is the template; a savepoint only when the
  caller keeps using `db` afterward. Before `pytest.raises` on any DB failure — a trigger's
  own RAISE included — check `__mro__` and run it for real.

## A service meant as THE one write path for future callers locks its row, even with one caller today

- **Rule:** A function documented as "the ONE way" something gets written locks its row
  (`with_for_update=True`, **and `populate_existing=True` with it**) before checking and
  writing; a plain read stays lock-free.
- **Why:** `applications.service.set_status` read `Application` unlocked: two READ
  COMMITTED callers moving one row off `INVOICED` (a scheduler job, a payment callback)
  both passed validation, and the second UPDATE silently overwrote the first (review C1) —
  the same shape `notifications.service._deliver`'s `with_for_update` already fixed.
- **The lock alone is half the fix (3.9a review C2):** the loader repopulates only
  *unloaded* attributes of an instance the session already holds, and
  `expire_on_commit=False` never expires them — a caller running `service.get(...)` first
  validates a STALE status under a correct lock. Now mechanical:
  `test_every_locking_get_also_repopulates_the_row`.
- **How to apply:** Give the write path its own locking repo read
  (`get_application_for_update`). Proving a lock needs two sessions
  (`make_session_factory(engine)`, template in `test_public_surface.py`):
  `asyncio.create_task` + `not task.done()` while the first stays open, then commit and
  assert the second raises.

---

# Values: dates, decimals, precision

## Business dates come from `business_today()`, never `date.today()`

- **Rule:** Anything gating on "today" — validity windows, expiry, seasons, permit terms —
  uses `app/core/time.py::business_today()` (Asia/Tashkent).
- **Why:** `date.today()` follows the SERVER's zone; on a UTC container it reports yesterday
  for ~5 hours a day. An expired fixed-term account could still authenticate 00:00–05:00
  Tashkent (`527d4e0`); the classifier read path had the same bug.
- **How to apply:** Enforced by
  `tests/test_code_conventions.py::test_no_module_under_app_calls_date_today`, so the
  live question is the one it cannot see: a `date` PARAMETER (`numbers.next_public_number`'s
  `on_date`) must say in its docstring that `business_today()` is where it comes from.
  Storage stays UTC `timestamptz`; only the *calendar-day decision* is Tashkent.

## A row in memory is not what Postgres stored — in your own session, or a different one

- **Rule:** `await db.refresh(row)` before trusting an in-memory row: in your OWN session
  after `flush()`/`commit()`, when what you serialize is a column the DB itself decides
  (`onupdate=func.now()`, a caller value in a fixed-scale `NUMERIC`); in a DIFFERENT session
  (the app's, from a test), before ASSERTING on a row a request may have changed.
- **Why:** one mechanism, two directions. SQLAlchemy fetches `onupdate` via `RETURNING` on
  INSERT but leaves it expired after a plain UPDATE — reading it outside the session's
  async context raises `MissingGreenlet` (`archive_template`, on `updated_at`). The INSERT
  side mirrors it: `RETURNING` covers only what the DB generates, so posting `"1.5"` into
  `Tariff.coefficient numeric(12,6)` left the row showing `"1.5"` while Postgres held
  `"1.500000"` (3.7 t3). Cross-session it fails silently: `service.get(db, id)` is
  `db.get`, no SELECT for a row already in the identity map, so two permits tests asserted
  `PAID` after the app wrote `PERMIT_ISSUED` (3.11a t4).
- **How to apply:** Refresh whenever a service both mutates/creates such a column AND
  returns it. Route test assertions through ONE refreshing helper
  (`permits/test_signatures.py::_reread`). A `Decimal` field backed by `NUMERIC(p,s)` needs
  a `field_serializer` doing `format(value, "f").rstrip("0").rstrip(".")` (`_trim_decimal`)
  — never `Decimal.normalize()`: `Decimal('100.0000').normalize()` is `Decimal('1E+2')`.

## `Decimal` ordering comparisons raise on NaN, not just construction

- **Rule:** A validator that parses a `Decimal` from user input and then bounds it must keep
  the comparison INSIDE the same `try`/`except InvalidOperation` as the parse.
- **Why:** `Decimal("NaN")` parses fine; the ordering operator one line later raises.
  `InvalidOperation` is an `ArithmeticError`, not a `ValueError`, so pydantic never converts
  it to a 422 — it escapes as a 500. Hit twice on the same field: I7's fix wrapped only the
  parse, and the scoped re-review caught `"NaN"` reaching `POST /tariffs` as a 500.
  `"Infinity"` is unaffected — ordering against infinity never raises — which is why the
  split reads as "it already works" until tried.
- **How to apply:** `norms/schemas.py::_benefit_modifiers` is the shape — parse and bound
  share one `try`. Grep for the same split before adding the next `Decimal`-bounded validator.

---

# JSON boundaries

## Nothing in this app configures a JSON encoder — coerce before every JSON boundary

- **Rule:** Any value that is not a JSON primitive (`Decimal`, `date`, `UUID`, an
  `UploadFile`) is coerced at the single point where it crosses into JSON — a JSONB bind, an
  `err(..., details=...)` payload, a persisted request body. Never assume something
  downstream will encode it.
- **Why:** Three boundaries, three 500s, one absent encoder:
  - **JSONB bind** — the engine sets no `json_serializer`, so a `Decimal`/`date` value in
    `notifications.params` raises `TypeError: Object of type Decimal is not JSON
    serializable` at flush, INSIDE the caller's transaction.
  - **`DomainError` details** — `app.main`'s handler renders with stock `JSONResponse`,
    unlike a `response_model` route; `ERR-GIS-003`'s raw `Decimal`/`UUID` raised inside the
    handler itself, turning a clean 422 into a 500 (3.6a t5).
  - **A parsed form body** — a multipart file part is an `UploadFile`, not a string, so the
    Eskiz callback's dead-letter payload 500'd on a plain curl against the route that
    exists to survive garbage.
- **How to apply:** `gis.checks.jsonable` is the template; form dicts get `{k: v if
  isinstance(v, str) else f"<{type(v).__name__}>" for k, v in form.items()}`. A coercer
  whose callers need IDENTICAL conversions belongs in ONE function — the gis pair had
  diverged (no `UUID` branch), and `norms.calculator` has since grown a third copy.
  `_json_safe` for audit snapshots is deliberately different, and exempt.

---

# Permissions, roles, transitions

## An access rule has ONE source, and every path that answers it derives from there

- **Rule:** Permission, zone and "which named official" are separate questions, and each has
  exactly one source. Every path that answers a question — enforcing it, reporting it, or
  merely reading the row — derives its answer from that source, never from a second list
  written beside it.
- **Why:** Three drifts. `require_permission` answers "may this role at all", `zone_filter`
  "on whose rows": `GET /admin/users/{id}/permissions` had the first, not the second, so a
  regional admin read another region's user (3.3b). `sys_admin` skips `require_permission`,
  but `GET /auth/me` reported an empty `permissions[]` for one (3.3a). And
  `permits._readable_permit` admitted only the holder or `permits.view_any`, while
  `add_signature` admitted the three required signers — so the head, chief forester and
  accountant could SIGN a permit they could not OPEN, found only by an end-to-end run
  (3.11a).
- **How to apply:** Adding a path near a guarded one, copy its scoping, not just the
  permission code, and add a cross-zone/cross-org denial test. When a read and a write
  guard the same object, the read derives its rule from the write's own sources — the
  permits fix intersects `required_purposes()` with `signers.required_role()`.
- **Across a module-level boundary, the source moves down, never copies:** `norms` (level
  2) needed applications' "who forwarded it" fact for ruling #107, but couldn't import it
  from `applications` (level 3) — fixed by moving the constant and predicate to the lowest
  module both call downward (`audit`, level 0), not copying it or bolting it onto ruling
  20's unrelated grant (F7, `docs/plans/07.4-findings.md`).

## A role's identity and its grants have ONE source — the seeding migration, never a name or a docstring standing in for it

- **Rule:** Before seeding a role-based grant, or writing a `_client_for(db, ...)` fixture for
  a role, read the actual migration — `0003_auth.py` for `roles.code`, the granting migration
  for what it holds — never a plan's role name, never a fixture's docstring.
- **Why:** two shapes. A WRONG CODE seeds nothing, silently: `plans/03.6a-gis-core.md` used
  `rahbar`, but there is no such `roles.code` — **«Раҳбар» is `executor_head`** (the leshoz
  head), not `leadership` (view+export plus `norms.publish` alone). `INSERT … WHERE code =
  'rahbar'` inserted zero rows, so `gis.contours.approve` reached nobody (3.6a t1);
  resolving it to `leadership` then left the leshoz head unable to approve anything in its
  own leshoz for two stages (fixed by 0016). A MIRRORED GRANT goes stale instead:
  `leadership_client` (3.7 t4) held `NORMS_APPROVE, NORMS_MANAGE` under "approves, never
  publishes" — true of the OUTCOME, wrong about the GRANT (0011 gives `leadership`
  `norms.publish` too) — a test expecting `ERR-ACL-002` got `ERR-ACL-001`.
- **How to apply:** Any role grant or fixture → `test_permissions_registry.py`'s two guards
  already assert the `leadership`/`executor_head` split, extend them. A wrong seed inserts
  zero rows, never an error; a docstring can describe the OUTCOME while omitting a GRANT.

## A maker-checker route needs BOTH roles' permission — the service tells them apart

- **Rule:** When the real gate is "not the same person who did the earlier step", the route
  takes `require_any_permission(EARLIER_CODE, LATER_CODE)`; the identity check belongs in the
  service, AFTER the permission gate, not instead of it.
- **Why:** `norms.service.publish_versioned` refuses a maker publishing their own draft by
  comparing `created_by` to `actor.id` — which only runs if the dependency lets the request
  through. Gating `/publish` on `TARIFFS_PUBLISH` alone gave `tariffs_maker_client` a 403
  `ERR-ACL-001` instead of the service's 409 `not_maker_checker` (3.7 t3).
- **How to apply:** If a service's refusal is an identity comparison rather than a status
  check, gate the route across every role that can legitimately reach that step.
- **The mirror, same task:** widening does NOT extend to siblings sharing the gate.
  `archive_versioned` has no `created_by` — archiving is single-actor — so the same
  `require_any_permission` on `/archive` let any `TARIFFS_MANAGE` holder pull a published
  row out of force: a silent 200 instead of a loud wrong status. Fixed by keeping the route
  wide, adding an in-handler check firing only when `published` — check every SIBLING route
  for the same gap before widening a gate.

## A status-transition table is ambiguous wherever two source states share a target

- **Rule:** When a target is reachable from more than one source, EVERY route driving any of
  those edges must assert the SOURCE state too — the new route and the one already there.
- **Why:** `return-to-review` (`CONTOURS_APPROVE`) and `submit-review` (`CONTOURS_MANAGE`)
  both land on `review`. Checking the target alone let the approver drive `draft → review`
  under the wrong permission. The mirror stayed open after that fix: `submit_review` kept the
  bare check and drove `approved → review` — the rework edge — auditing it as `submit_review`
  (3.6a fix wave).
- **How to apply:** Grep `TRANSITIONS` for the target before adding a route: more than one
  source means every route reaching it needs `_assert_transition_from(...)`. Fix the siblings
  in the same commit.

## An existence check is not a validity check

- **Rule:** Verifying that a referenced object EXISTS says nothing about whether it is
  legitimate. State which one you did, in the docstring, at the call site.
- **Why:** Power-of-attorney representations validate that the poa file exists, is owned by
  the caller and is a PDF (3.3b) — none of which is proof of a valid power of attorney. The
  staff-review ruling is still open, parked before 3.9.
- **How to apply:** When a check is only structural, say so where it is called, so the next
  agent does not read it as authorization.

---

# Root fixes: one place, not many

## A precondition shared by several steps belongs in ONE function every step calls

- **Rule:** A precondition belonging to a WHOLE transition lives in one function every step
  calls, never only in the step a well-behaved caller reaches first — and a transition whose
  loop moves zero child rows is refused, not advanced.
- **Why:** `submit_import_review` alone got the "refuse a non-contour batch" guard;
  `approve_import`/`publish_import` still gated on `row.status`, and `CONTOURS_APPROVE` is a
  DIFFERENT permission — so a rahbar-only actor called `/approve` on a freshly-parsed batch,
  both loops moved zero rows, and the status advanced anyway (3.6a t8; 200 instead of 409).
- **How to apply:** Factor the shared preamble — row lookup, zone check, validity check,
  status check — into one function. A loop finding nothing is not evidence that nothing
  needed to happen.
- **The mirror, 7.4 F3 — widening one function is a decision about every caller.** Ruling
  #113 added `address` to `checks.missing_for_pricing`, correct by that function's
  contract; but `service.precheck` reads the SAME list to decide whether there is a price
  to show, so an address-less citizen reached the wizard's last step and would have signed
  an ERI signature over a package whose cost was never shown — green in every track's own
  suite, found only on the integration branch. List the callers before you add.
- **The same discipline on inputs:** before calling a versioned-row creator done, walk
  every caller-settable FK or period-pair field and confirm EACH has a service guard ahead
  of `flush()`. Task 4 guarded `contour_id`/`approval_doc_id` while `activity_type_id`,
  `geobotanic_doc_id` and `effective_to < effective_from` reached `flush()` as
  `ERR-SYS-001`/500 — the same gap sat in the SHARED `create_versioned`
  (`add_classifier_item`'s period check and `_assert_doc_active` are the reusable guards).

## A new refusal on a hot path breaks every caller through a seeded row, not through code

- **Rule:** Adding a precondition to a widely-shared entry point (a JSON-RPC dispatcher, a
  webhook handler), check what a FRESH TEST DATABASE's seeded rows make TRUE by default —
  not just the callers your diff touches — before trusting a file-scoped green run.
- **Why:** Stage 7.9 t6's Payme routability check broke ~15 PRE-EXISTING tests across three
  unrelated files (none about routability) because migration `0045`'s seeded `budget_50`
  recipient has no Payme id BY DESIGN and is active in every fresh test DB — every real
  invoice built through it became "unroutable". Every file-scoped run stayed green; only
  `make test` on the WHOLE suite showed it.
- **How to apply:** `tests/modules/payments/conftest.py::_budget_recipient_is_routable` is
  the fix shape — a package-level autouse fixture giving the seeded row a fixed test value
  via `engine` (never `db`, whose rollback never reaches the app's own connection).

## A gate that reads only ONE of the two things it guards is bundling two concerns

- **Rule:** When an `if` guards a block computing several values, check that EVERY value in
  the block reads something from the condition itself. A value computed without ever touching
  the condition's subject is gated on the wrong thing, even if it is correct today.
- **Why:** `calculator.calculate` computed `used_sb` (a REQUEST fact — `request.items` ×
  `coef_sb:<code>`) inside `if snapshot.norm is not None:` (a NORM fact). Task 7 needed the
  same number with no norm yet — a real, supported case — so a preview for a fresh contour
  silently returned `used_sb=None` instead of the missing-parameter error. The first fix
  added a SECOND resolution of the same lookup in the service: two places deciding one
  number, agreeing only by luck.
- **How to apply:** Split the gate at the root — `if request.activity_code == GRAZING:` for
  `used_sb`, `if snapshot.norm is not None:` for `max_sb`/`remaining_sb`. Before adding a
  caller-side workaround for a gap in a shared function, check whether the gate reads what it
  claims to gate on: a condition never referenced inside its own block is the tell.

## A cached fact must be re-validated wherever its own source can later change

- **Rule:** A column deriving from X, once set trusted to diverge from X on purpose (an
  assignment), must be RE-checked against X — not just checked for presence — wherever a
  LATER feature could make X editable again.
- **Why:** `assigned_org_id` derived from the contour's owner until assigned, then was
  trusted unconditionally; a later task made RETURNED editable, so a corrected `contour_id`
  onto another leshoz kept the FIRST leshoz assigned, invisible to either task alone (3.9b).
- **How to apply:** Before trusting "already set", re-derive from source and compare.

## A reversed date period inverts a range predicate and hides the rows it should find

- **Rule:** Any function that walks a period day by day, iterates its years, or passes both
  ends into a SQL overlap predicate rejects `period_to < period_from` **at the shared entry
  point**, fail-closed, before anything runs — never trusting the caller's schema to have
  ordered them. Same for an unbounded period.
- **Why:** With the dates swapped, `norms.checks`'s own walks no-op and report `pass` having
  examined nothing — bad, but visible. The real damage is `gis.service.features_intersecting`,
  whose predicate `valid_from <= :period_to AND valid_to >= :period_from` then fails BOTH legs,
  so a fire ban genuinely covering the requested days drops out of the result set and the one
  blocking check this stage exists for returns a confident `pass` (3.7 t6).
- **How to apply:** Guard in the module's public entry point (`run_checks`), not each
  router's schema. `ERR-VAL-001` with `period_reversed`/`period_too_long`; the ceiling is a
  named constant (`MAX_PERIOD_DAYS`). `from == to` must still pass.

## A narrow helper without full context signals a refusal back, it does not raise on partial evidence

- **Rule:** When a helper lacks the object/purpose/context a refusal's evidence row would
  need, don't widen its signature or raise from inside it — leave the affected state
  caller-detectable (e.g. an unset FK) and let the full-context caller override its own
  result through the evidence-then-raise tail it already has.
- **Why:** `signatures.bind_certificate(db, *, info, user)` has no `object_type`/`purpose`,
  so it cannot itself write a `signatures` row for "certificate owned by someone else"; it
  leaves the certificate UNBOUND and returns, and `sign()` downgrades its own `Verdict` to
  `invalid`, reusing the one insert/audit/commit/raise tail every refusal already goes
  through — zero duplicated evidence-writing code (3.8, fix rounds 1 and 3).
- **How to apply:** Before widening a helper's parameters so it can raise directly, check
  whether its caller already has an evidence-then-raise tail the helper could feed via a
  `replace`-able result object instead.

## A cap borrowed from a sibling module inherits its volume assumption, not its shape

- **Rule:** Copying a size/count cap from another module, re-justify the volume it assumes —
  a cap the source could defend does not transfer with the number alone.
- **Why:** The invoice register borrowed a 500-row zone-scan cap from the manual-confirmations
  queue ("a maker files these one at a time, a real worklist is small") — invoices are the
  system's core document, generated per approved application nationwide. Past 500 a leshoz saw
  ZERO of its own invoices, permanently, and the total silently undercounted — the failure
  hides data rather than leaking it, so nothing alarmed.
- **How to apply:** Reusing a cap, ask whether the source's own justification holds for the
  new caller; where the real count can exceed it, count/page in SQL, never truncate silently.

## A module's hard-coded claim about another module rots silently — its own tests will not catch it

- **Rule:** A hard-coded fact about ANOTHER module's state ("not merged yet", "no such table")
  needs a check that fails once the fact goes stale — never a comment trusted to be re-read.
- **Why:** `dashboard/service.py` hard-codes `OMITTED_TILES` saying `inspections` "is not
  merged into `dev` yet" — true when written; `inspections` has since merged with 21 routes and
  a real `violation_cases` table the dashboard still refuses to count. Its tests pass because
  they assert `omitted` is reported HONESTLY — nothing asserts the REASON still holds.
- **How to apply:** Hard-coding an omission tied to another module's absence, add a test that
  fails once that module ships, or tie it to a tracked ticket.

## A guard that corrects a row's own column must be read back from that column, not from the raw answer it was given

- **Rule:** After calling a step whose job is "write the reconciled value onto this row", every
  later decision in the same function reads the ROW's own field again — never the raw parameter
  handed to that step, even a few lines below the call.
- **Why:** `signatures.service._reconcile_status` refuses to un-revoke `cert.status` from a
  date-only adapter's answer; `reverify()` built its verdict from that SAME raw `live_status`
  anyway, so a revoked certificate's signature would have re-reported "valid" the moment a
  real-mode adapter confirmed only the expiry date (5.2, caught pre-merge).
- **How to apply:** A function calling a reconcile/guard step on `row.field`, grep its own body
  for the parameter name it passed in — every use after the call is a bug; re-read `row.field`.

## A `.get(key, default)` over a GROUP BY turns another module's retired enum value into a plausible zero

- **Rule:** A dict built from a `GROUP BY` and read with `.get(key, default)`, where `key`
  is another module's enum-ish string (`target`, `status`, `kind`), must not default
  silently — assert the key set against the column's CURRENT check/tuple instead.
- **Why:** Stage 7.9's migration `0046` retired `allocations.target = 'budget'`, rewriting
  every row to `'receiver'` and dropping it from the CHECK. `dashboard/repo.py::payments_kpi`
  — a DIFFERENT module — still read `by_target.get("budget", Decimal("0.00"))`; nothing
  carries that key after the migration, so `budget_share_amount` reported zero with no
  exception, no log line, and no test — found only by a human grepping the retired string.
- **How to apply:** Aggregating another module's enum-ish column into a dict, grep every
  `.get(<literal>,` keyed by it and pin the key set against the owning table's CHECK — the
  shape `test_the_schema_literals_match_the_tables_own_check_constraints` already uses.

---

# PostGIS

## A geometric predicate lies at its edges — decide the degenerate case explicitly

- **Rule:** A "do these overlap" check over real polygons never trusts the bare boolean —
  compute the intersection AREA against a named tolerance. A containment check whose
  reference layer might still be EMPTY needs a THIRD outcome, `skipped`, decided up front.
- **Why:** two edges, one naivety. `ST_Intersects`/`ST_Overlaps` is `true` for a zero-area
  shared border exactly as for a genuine double-booking — two neighbouring published
  contours sharing a fence line are NORMAL on real cadastral data, so the spec's own
  `ST_Overlaps` check would have refused valid neighbours. And `gis.checks._within_fund`
  against an empty fund-boundary layer would report EVERY contour as `outside_forest_fund`
  — blocking every publication for as long as the Agency's delivery is pending, without an
  explicit `skipped`/`layer_empty` branch.
- **How to apply:** `gis.checks._intersections` computes
  `ST_Area(ST_Intersection(a,b)::geography)` against `gis_overlap_tolerance_m2` (default
  100 m²), proven by `draft_version_touching_it` vs `draft_version_overlapping_it`. A check
  whose reference layer isn't yet populated gets its own `count(*) == 0` branch, returned
  as a named result — it turns itself on the day the data lands, no code change.

---

# HTTP layer

## The request's transaction commits before the response is sent, not after

- **Rule:** Leave the request's `commit()` in `CommitBeforeResponseMiddleware`
  (`app/core/deps.py`), which fires on `http.response.start`; `get_db` only rolls back and
  keeps a fallback. Moving it back into `get_db`'s `else:` restores the race below.
- **Why:** FastAPI (>= 0.106) exits a `yield` dependency AFTER the response reaches the
  server, so a client racing its own write loses: on dev 2026-09-09 `POST /auth/login`
  answered 200 with a session cookie and a request 6 ms later got `ERR-AUTH-002` — which the
  adminka reads as "session gone" and bounces to /login. The window shut by ~40 ms, so every
  retry worked and no test saw it.
- **How to apply:** `tests/core/test_transaction_timing.py` pins the ORDER of `after_commit`
  against `http.response.start`. A probe reading the row from a second connection CANNOT: its
  own `await` lets the loop finish the very commit it means to catch, and reports "no race".

## `request.body()` raises inside a dependency on any FORM route

- **Rule:** A dependency needing the raw body (`auth.deps.idempotency_context` is the only
  one) branches on CONTENT TYPE: for `multipart/form-data` and
  `application/x-www-form-urlencoded` it reads `await request.form()`, never `request.body()`.
- **Why:** FastAPI reads the body before solving dependencies and, for a form, parses straight
  off the stream rather than caching bytes — so `Request.body()` re-enters `Request.stream()`,
  hits `_stream_consumed` and raises `RuntimeError("Stream consumed")` as an unhandled 500.
  Hit when `POST /gis/imports` became the idempotency mechanism's first consumer (3.6a).
- **How to apply:** The fingerprint for a multipart route is the scalar fields plus each
  file's name/filename/content-type/size, sorted — never the file bytes. **The mirror is a
  trap:** the branch keys on content type, not route, so a JSON route declaring
  `idempotency_context` that is SENT multipart takes the form branch and FastAPI's own
  `request.body()` then raises the same error. Not reachable today; a route accepting both
  shapes needs the branch reconsidered, not reused.

## A `response_model` mismatch is invisible to ruff and pyright

- **Rule:** After writing any route with `response_model=`, hit it once for real before
  trusting a brief's snippet verbatim — pyright checks the handler body, never whether the
  returned value satisfies the declared model at runtime.
- **Why:** Two defects in the same APPROVED plan (`03.7-norms.md` t3), both past `make check`:
  `Page[T]` requires `page`/`page_size` but the plan's snippets returned
  `{"items","total","limit","offset"}` → `ResponseValidationError: Field required` on every
  call; and `PublishOut.item: Any` held a raw ORM row → `PydanticSerializationError: Unable to
  serialize unknown type`, because `Any` gets NO from-attributes treatment the way a concrete
  `BaseModel` field does.
- **How to apply:** A list endpoint whose repo takes `limit`/`offset` still builds `Page[T]`
  with explicit `page = offset // limit + 1`, `page_size = limit`. Any envelope with an
  `Any`-typed field that might hold an ORM row needs `SomeOut.model_validate(row)` at the
  call site.

## Two paging conventions in one codebase, and the wrong one fails silently

- **Rule:** Reuse the project's one paging convention (`PageParams`, `?page=&page_size=`); a
  route that must diverge (`norms`'s `?limit=&offset=`) calls that out at the route itself.
- **Why:** FastAPI ignores an unknown query parameter, so paging `norms` the way every other
  route pages gets NO error — it returns page one, forever. No mocked front-end test catches
  it either, since the mock answers whatever the client sends.
- **How to apply:** Before adding a list endpoint, grep for the existing paging convention and
  reuse it; a deliberately different one needs a docstring callout and a test proving the
  wrong convention is at least detectable, not silently wrong.

## A cap checked after reading the body is not a cap — and an anonymous route must cap what it PERSISTS

- **Rule:** Enforce a size limit from `Content-Length`/`UploadFile.size` FIRST, then
  chunked-read with a running total. Separately, any column an unauthenticated caller can
  fill gets an explicit size cap with a truncation marker.
- **Why:** The 3.3b upload path read the whole body into RAM before comparing to
  `max_upload_mb`: enforced, but memory was already spent. `inbound_dead_letters.payload`
  stores an arbitrary-size Eskiz-callback body while `error` beside it is capped to 1000
  chars — no body-size middleware or purge job exists for dead letters. "Every other
  endpoint does the same" was the wrong defence: those don't PERSIST the body.
- **How to apply:** Every future ingest path (attachments 3.9, geodata import) caps early;
  `service.DEAD_LETTER_PAYLOAD_MAX_BYTES` is the persisted-cap shape, and the stored row says
  it was capped. `max_upload_mb` also has to be mirrored by the proxy's `client_max_body_size`.

## `Content-Disposition` filenames must be RFC 6266/5987-encoded

- **Rule:** Emit both `filename="<ascii fallback>"` and `filename*=UTF-8''<pct>` — never
  interpolate the raw name.
- **Why:** A Cyrillic or Uzbek-Latin-with-diacritics filename made `GET /files/{id}` return
  500: the header must be latin-1 encodable (3.3b, `aa1d551`). Our users upload exactly such
  filenames.
- **How to apply:** Any new download endpoint reuses the helper in `app/core/files.py`.

## `secrets.compare_digest` raises `TypeError` on non-ASCII strings

- **Rule:** Compare secrets as BYTES — `compare_digest(a.encode(), b.encode())` — whenever
  either side can come from a URL path, header or query string.
- **Why:** `POST /api/v1/webhooks/eskiz/%CE%A9` hit the blanket 500 handler instead of the
  intended 404, on the one route whose stated invariant is that it never 500s on garbage
  (3.5 final review). The str form accepts ASCII operands only.
- **How to apply:** Every future provider webhook (Payme 3.10, my.gov.uz later) compares
  bytes and gets a non-ASCII-path test beside its wrong-secret test.

---

# Outbox and integrations

## An outbox sender's return/raise choice IS the retry decision

- **Rule:** A registered sender RETURNS when a failure is permanent and RAISES only when a
  retry could plausibly help — `deliver_one` retries on any raised exception and treats a
  normal return as delivered. Never let one boolean stand for a permanent and a temporary
  reason at once.
- **Why:** Raising for an unreachable recipient burns `outbox_max_attempts` and the circuit
  breaker for the same outcome, holding back every other message on that destination. Worse
  (3.5 final review): one helper answered both "is this recipient reachable" (permanent)
  and "is the ops kill switch on" (temporary), so flipping `notifications_sms_enabled` off
  for an hour DESTROYED every queued SMS blaming the recipient — unrecoverably, since admin
  requeue only works on `dead` rows.
- **How to apply:** Before writing `raise` in a sender, ask "would a second attempt with the
  same input succeed?" If no, set the terminal status yourself and `return`. A condition an
  operator can reverse must RAISE.

## Senders registered at module import must be imported by the standalone worker too

- **Rule:** A destination registered via `register_sender(...)` at import time exists only in
  a process that imported that module — add the import to `app/workers/outbox.py` explicitly,
  with a comment, in the same commit.
- **Why:** In tests and in the embedded deployment, `app.main` imports the routers, which
  transitively import `notifications.service`, so registration "just happens". A standalone
  `python -m app.workers` imports neither, so every notification goes `dead` as
  `unknown destination` — and no in-process test can catch it.
- **How to apply:** New destination → grep `app/workers/outbox.py` for the registering import
  → add it → extend the subprocess registration test.

## A sender's own diagnostics must never carry what it was sending

- **Rule:** A sender may report the TRANSPORT failure (status code, provider error code) in a
  raised exception — never the payload's contents. And it may request a delivery-report
  callback only for a send that has a stored row to correlate it against — pass an explicit
  "no callback" flag otherwise.
- **Why:** two shapes, one sink. `outbox_messages.last_error` is admin-visible AND logged,
  so a sender formatting the payload into its exception would leak live OTP codes to anyone
  holding the admin outbox permission. And `EskizSmsSender` put `callback_url` in every
  payload while `RealOtpSender` passed a throwaway uuid, so at `sms_mode=real` EVERY OTP
  would produce an `inbound_dead_letters` row holding the recipient's phone number, forever
  (3.5 final review).
- **How to apply:** Every new sender raises with transport metadata only, plus a test
  asserting the code is NOT in `str(exc)`. Wiring a provider callback, ask what the DLQ does
  with a report matching nothing — and remove the cause rather than filtering it.

## `str.format` on admin-authored text is an attribute-access hole

- **Rule:** Never render user- or admin-authored template text with `str.format` or an
  f-string; substitute placeholders with a whitelist regex (`\{([a-z][a-z0-9_]*)\}`).
- **Why:** `"{x.__class__}".format(x=obj)` reaches Python attributes on whatever is passed in,
  and `.format()` raises `KeyError` on a placeholder the caller forgot — inside a business
  transaction that turns an admin's typo into a failed application submission.
- **How to apply:** Anything a non-developer authors and the code renders (templates today;
  announcements, rejection reasons, report labels tomorrow) goes through
  `notifications.service.render`'s pattern.

---

# Tests and the shared test DB

## A check-then-create against a shared resource is a race the day the suite runs `-n 4`

- **Rule:** "Look, then create" against MinIO, a volume or a catalog must treat the
  already-exists answer as SUCCESS. Prove it with a CONCURRENT test: a sequential
  idempotency test never reaches the create branch at all.
- **Why:** `storage.ensure_bucket` did `head_bucket` → on `ClientError` → `create_bucket`.
  On a FRESH MinIO — every CI run — four xdist workers 404 together, all four create, and
  the losers got an uncaught `BucketAlreadyOwnedByYou`: `ERROR at setup` on an unrelated
  test, and a red CI accusing whichever branch was in front of it. Never reproduced
  locally, where the bucket already exists and the create is never reached (2026-09-06).
- **How to apply:** Any `ensure_*` or first touch of a fresh volume → catch the
  exists-codes (`storage.py::_BUCKET_ALREADY_THERE`), and copy the four-worker concurrency
  test: gather four calls against a uniquely named bucket, never delete the shared one.

## The test DB is shared, persistent, and never empty — including the spot you picked

- **Rule:** A test may only touch rows it created. No unscoped `UPDATE`/`DELETE`, no assuming
  an empty database, and no assuming an empty *neighbourhood*.
- **Why:** The DB is shared across worktrees and runs, fixtures leave rows behind forever,
  and the round-trip test wipes it wholesale. Four consequences, each with its remedy:
  - **A fixed literal accumulates:** `box_wkt(69.9, 41.5)` held 11 stray contours, a
    hard-coded `permits.number=1` dies once issuance commits one → randomise
    (`random_box_wkt()`) unless a sibling needs proximity (`neighbouring_published_contour`).
  - **A claim-the-oldest worker takes a stranger's row:** `process_pending` claims the oldest
    `pending` import in the DB, not yours → a package-scoped autouse drain running the JOB,
    bounded by `DRAIN_LIMIT`, never a DELETE.
  - **A global number is never your number:** page 1 is full of previous runs, and a
    whole-table sweep returns a whole-table count — `sweep_overlapping_permits` scans every
    permit pair in the DB, so `assert written == 1` measured other suites' permits too,
    going red after a prior green run (`7 == 1`; PR #75) → assert on rows carrying your
    fixture's own ids (`details["contour_id"]`, a fresh `organization_id`).
  - **A refused action leaves its row:** `test_a_maker_cannot_archive_a_published_tariff`
    succeeds BY being refused, so its `science` tariff stays published forever → a
    yield-fixture teardown with a scoped DELETE; a row the test REFERENCES (FKs,
    append-only history) can't be deleted at all → get-or-create with fixed ids.
- **How to apply:** Scope every assertion by the ids your fixture created. Run any new
  negative test twice in a row, and as part of the FULL suite — this class is invisible in
  isolation. Do not reorder the conftest collection hook.

## A module's test conftest needs plumbing copied from an existing one, not just fixtures

- **Rule:** Writing a module's first HTTP-driven test file, copy three things from an
  already-HTTP-tested package before the first `client.get(...)`: the autouse
  `_app_on_test_db` guard, `_commit_pending_before_requests` on every client fixture, and
  `from module import name as name` for a fixture re-exported AND consumed locally.
- **Why:** each one fails for a reason with nothing to do with the assertion under test.
  - **No `_app_on_test_db`** (monkeypatch `DATABASE_URL` to `database_url_test` +
    `get_settings.cache_clear()`, never inherited from another package): `create_app()`'s
    lifespan opens the shared DEV database, every session cookie a fixture wrote is
    invisible to it, and EVERY request 401s — reading as a blanket auth failure, not a DB
    mismatch (`signatures/test_api.py`, 3.8 t7).
  - **No commit hook:** pytest instantiates fixtures LEFT-TO-RIGHT (verified), so in
    `test_x(gis_client, leshoz, ...)` the client's own setup-time commit runs before
    `leshoz` executes — its `flush()`-only row stays invisible to the app's separate
    connection and the test FK-fails. Never rely on parameter order.
  - **Plain re-export:** pyflakes flags a parameter shadowing an "unused" import as F811,
    silent when the earlier binding is a locally-DEFINED fixture — `norms/conftest.py`
    re-exporting gis's four failed on all four (3.7 t1). Fix: `# noqa: F401` for
    re-exported-only names, `as <same name>` for ones also consumed.
- **How to apply:** `tests/modules/gis/conftest.py` is the template for all three. Grep a
  new package's own `conftest.py` for `_app_on_test_db` and `_commit_pending_before_requests`
  and add them there (autouse for the first), never per-file.

## Build a fixture's precondition through the real transition, never by assigning the status

- **Rule:** A fixture that needs a row in some state reaches it by running the code that
  produces that state — publish the event, call the service — never by writing
  `status="X"` on a freshly constructed row.
- **Why:** 3.10a's `pending_invoice` hand-set the application to `APPROVED` while an
  application holding an invoice is already `INVOICED`. `APPROVED -> PAID` is not a legal
  jump, so `PerformTransaction` was refused with `ERR-APP-004` and three tests failed
  pointing at the payment code — the FIXTURE was wrong. Worse, had the transition itself
  regressed, a hand-set status would have hidden it and every test stayed green — same
  reason `cancelled_invoice` cancels through the real subscriber.
- **How to apply:** Writing a fixture whose docstring says "an application already in X",
  ask which call puts it there and make the fixture do that; `tests/modules/payments/
  conftest.py`'s `pending_invoice`/`cancelled_invoice` are the template.

## A conftest autouse fixture runs before your test — schema checks belong in the gate

- **Rule:** A check about Alembic or the schema itself (single head, revision order) goes into
  `make check` / CI / pre-commit, never into a pytest test expecting to report the failure.
- **Why:** `tests/conftest.py::_migrated_test_db` is `scope="session", autouse=True` and runs
  `alembic upgrade head` before ANY test, so with two heads it raises first and a
  `test_single_alembic_head` never reaches its own assert — pytest reports alembic's message
  instead of the actionable one (verified 2026-08-29 by planting a second head).
- **How to apply:** `make heads` covers this one. Before writing a test about infrastructure
  the fixtures themselves depend on, ask which runs first.

## A "public surface" task's own end-to-end test can ship the surface untested

- **Rule:** When a task adds functions to a module's public surface FOR a caller that does not
  exist yet, check whether its end-to-end test actually calls them — an HTTP scenario
  exercises the ROUTES, not the in-process functions a future module will call.
- **Why:** Task 8's test drives `preview`/`save_calculation`/`publish_norm` over `httpx`,
  while `service.effective_norm` and `service.run_checks` — exactly what 3.9/3.11 will call
  in-process — were reached by nothing: hard-coding `run_checks`'s `used_sb` to a real Decimal
  left the brief-verbatim test green.
- **How to apply:** Add a direct in-process call to each NEW function inside the SAME test,
  reusing its committed fixtures, asserting it agrees with what the HTTP path proved. Never
  ship a contract function whose only verification is that it type-checks.

## A test left reading the real clock is a scheduled failure — the date OR the hour

- **Rule:** Freeze whatever the code under test asks the clock for, patching it in the CALLING
  module's namespace (`app.modules.norms.service.business_today`), never in `app.core.time`.
- **Why:** `0012` seeds `bhm` as two dated rows (412 000 until 2026-08-31, 440 000 after). 3.7
  t7 hard-coded amounts from 412 000 while `_compute` resolved `on_date=business_today()`:
  green the day it was written, red on 1 September with nobody touching the repository.
- **The hour of day, same class (#152):** the SMS quiet window made five delivery tests pass at
  09:52 and fail at 01:53 — nightly in CI, where nobody watches the clock. A feature gated on
  the wall clock needs the predicate patched OFF suite-wide (`tests/conftest.py::
  _no_sms_quiet_window`), with a marker opting its own test back in.
- **How to apply:** Adding a seed row effective in the future, or any `now()`/hour comparison,
  grep the suite for the in-force figure and for the clock call. Never "fix" such a test by
  recomputing from whatever is in force — that passes against a WRONG tariff.

## An uncommitted test setup on the same session gets committed for real by an expected refusal

- **Rule:** Never leave an uncommitted prerequisite (a settings override via
  `db.merge`/`flush`) on the SAME session earlier in a test whose next call is expected to
  raise via the early-commit-before-raise pattern (`CLAUDE.md`) — commit it separately
  first, or assert against the default instead.
- **Why:** A signatures test wrote an uncommitted `SystemSetting` override, then called
  `sign()` expecting `ERR-SIGN-001` — the refusal's own `db.commit()` commits EVERYTHING
  pending on that session, so the override persisted for real. Invisible alone; surfaced
  only running the whole file, because an earlier test had already cached the now-wrong
  default via `settings_store`'s 60-second cache (3.8 t6).
- **How to apply:** Before combining a settings-override write with a call that could be
  refused, ask whether that call's whole point IS the refusal — if so, keep the write out of
  that test entirely.

## An unannotated test fixture parameter hides a `str | None` argument-type error pyright would catch

- **Rule:** Annotate test function parameters with their real fixture type (`a_user: User`,
  not bare `a_user`) — pyright then checks attribute access against the actual model,
  catching a nullable-column mismatch an unannotated (implicit `Any`) parameter silently
  swallows.
- **Why:** `test_sign.py` calls `_pkcs7(a_user.pinfl)` (a `str`-only parameter) with zero
  pyright errors ONLY because its test functions never type `a_user` — `User.pinfl:
  Mapped[str | None]` needs narrowing. Adding `a_user: User` in a new file surfaced three
  real `reportArgumentType` errors for the identical expression (3.8 t5).
- **How to apply:** Prefer typed test parameters generally; narrow a fixture's nullable
  attribute explicitly at the call site (`assert a_user.pinfl is not None`) instead of
  leaving the parameter unannotated to dodge the check.

## An assertion over rendered output is testing this machine's fonts, not your code

- **Rule:** Reading text back out of a PDF, squeeze the whitespace from BOTH sides
  (`"".join(s.split())`). Never assert a substring against `extract_text()` verbatim.
- **Why:** the С22 watermark tests passed on macOS and failed in CI with `assert 'Test
  User' in '... T est User — 2026-09-07'`. `extract_text()` rebuilds words from glyph
  positions and inserts a space wherever a run is kerned, depending on the image's fonts —
  the negative assertion fails OPEN the same way, a leaked name slipping past on a space
  (2026-09-07). Mirror the same evening in the adminka: a mock using jsdom's `Blob` passed
  on Node 25, failed on CI's Node 22.
- **How to apply:** any test reading back what WeasyPrint produced —
  `tests/modules/search/test_export.py::_pdf_text` is the helper. Green locally and red in
  CI: reproduce the CI runtime first (a `node:22` container did it here).

---

# Tooling and environment

## Python 3.14: `except A, B:` without parens is VALID — check syntax with the project's Python

- **Rule:** Verify a file with `uv run python -m py_compile <file>`, never the macOS system
  `python3` (3.9). And do not "fix" `except A, B:` back to parenthesised form.
- **Why:** PEP 758 makes parenthesis-free multi-except legal and `ruff format` actively STRIPS
  the parens under `target-version = py314`; an older interpreter rejects it, which reads as a
  phantom SyntaxError blocker in review. *(from ControlAI, where it cost a PR review round.)*
- **How to apply:** Trust `uv run` — the venv is CPython 3.14. The formatter will undo a
  manual "fix".

## Log `repr(e)`, not `f"{e}"`

- **Rule:** In every `except Exception as e:` that produces a log line, log `repr(e)` (or
  `type(e).__name__` plus the message) and attach the traceback.
- **Why:** asyncio-flavour `TimeoutError` — `asyncio.wait_for`, an asyncpg pool acquire, an
  httpx timeout — has an EMPTY `str()`. The line becomes `"delivery failed: "` and the
  incident is undiagnosable without a redeploy. *(from ControlAI; our outbox worker, MinIO
  client and HTTP senders are exactly the code that times out.)*
- **How to apply:** structlog: `log.error("…", error=repr(e), exc_info=True)`.

## `.env.example` drifts silently — and an EMPTY value is not ignored

- **Rule:** Adding or renaming a `Settings` field means editing `.env.example` in the same
  commit and grepping it for the OLD name. Every line there carries a real value or is
  commented out — never left empty.
- **Why:** `app/config.py` sets `extra="ignore"`, so an unknown env var is dropped without a
  warning: the field keeps its default and the operator believes they configured it *(from
  ControlAI, where `.env.example` shipped `WHISPER_DEVICE` while the code read `STT_DEVICE`)*.
  The opposite failure is worse: `EMAIL_MODE=`/`SMTP_PORT=` fail validation outright and
  `ESKIZ_BASE_URL=` silently replaces a working default with `""`, so `cp .env.example .env` —
  the README's first step — would not start (3.5 final review).
- **How to apply:** The env name is the field name upper-cased. Prove a var lands with
  `uv run python -c "from app.config import get_settings; print(get_settings().<field>)"`;
  `tests/test_config.py::test_env_example_is_a_working_env_file` is the guard.

## pre-commit refuses to run while `.pre-commit-config.yaml` is modified-but-unstaged

- **Rule:** Never edit `.pre-commit-config.yaml` (or any tooling file) in a working copy
  another session is committing from — take a worktree. If pre-commit says *"Your pre-commit
  configuration is unstaged"*, find whose edit it is rather than reaching for `--no-verify`.
- **Why:** pre-commit refuses to run against a config it cannot trust, so EVERY commit in that
  tree is blocked — including commits from a session that never touched the file. Hit twice on
  2026-08-29 in the shared copy; one commit shipped with `--no-verify` to get out.
- **How to apply:** One worktree per session (root `CLAUDE.md`). If already stuck,
  `git stash push .pre-commit-config.yaml` in your own tree beats `--no-verify`; if you do use
  it, run `make check` by hand and say so in the PR.

## A vendor's own Dockerfile is the contract, not something to reconstruct from research

- **Rule:** If a vendor distribution ships its own Dockerfile, build from it, not from
  documentation guesses. For a thin jar, check `Class-Path` in `META-INF/MANIFEST.MF`
  for a `lib/` directory before assuming the jar runs standalone.
- **Why:** `deploy/eimzo/Dockerfile` (5.2 task 8), written believing the jar was
  unobtainable, never copied `lib/` (`NoClassDefFoundError` on `picocli.CommandLine`,
  before class loading finishes) and passed the config path as a bare positional
  argument instead of the `-Dproperties.filename=` system property the jar actually
  reads. The vendor's own zip ships the jar, `lib/` and a working Dockerfile together.
- **How to apply:** Search for the real artifact before declaring it unobtainable. Treat
  an inferred boot/health behavior as unverified until measured against the real jar —
  5.2's own prior report inferred a graceful `/ping`; measured, the process never binds
  the port at all with no VPN key, so a healthcheck-gated `depends_on` would block `api`.
