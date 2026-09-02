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

**The file itself is capped at 60 entries / 900 lines.** Short entries still add up, and
reading cost is how many times how long. Once the cap is reached a new lesson has to be
paid for by merging two old ones of the same class, or by deleting one whose trap a check
has since made impossible — that is the point, not an obstacle. A pre-commit hook,
`make check` and CI all enforce both limits; `make lessons-check` is the same check on
its own.

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

## A downgrade must delete whatever its upgrade made possible

- **Rule:** A migration that widens a CHECK, or seeds a row other tables will reference,
  owes its `downgrade()` the matching `DELETE` — written when the migration is written,
  not when someone finally hits it.
- **Why:** Two shapes, both already red in CI: `0006` widened `otp_codes.purpose` and
  restored the narrow CHECK against rows that already violated it; `0010` seeded the
  `gis.import.finished` template and deleted only the template, so
  `fk_notifications_template_id_notification_templates` broke the round-trip the moment
  task 7 actually sent the event — in a task that touched no migration.
- **How to apply:** Widening a constraint → data-cleanup statement in the downgrade.
  Seeding a template → `DELETE FROM notifications WHERE event_code = '<code>'` above the
  template delete. When the round-trip goes red in a task that changed no migration, look
  for the event that task started emitting.

## Multiple Alembic heads: resolve with an empty merge migration

- **Rule:** When two branches each add a migration, run `alembic merge heads` — never
  delete a migration or hand-edit `down_revision`.
- **Why:** Rewriting revision history breaks every environment that already applied the
  original revisions (dev DB, test DB, CI, later prod). *(from ControlAI; live risk here
  — Oybek runs parallel sessions.)*
- **How to apply:** `uv run alembic merge heads -m "merge"`, commit it, keep going.
  `make heads` is the gate; check `uv run alembic heads` after any rebase.

## The round-trip test's expected head version is a hardcoded string every migration must bump

- **Rule:** After adding a migration, update `tests/test_migrations.py::
  test_downgrade_upgrade_roundtrip`'s `assert version == "<old head>"` in the same commit.
- **Why:** The assertion is a literal, not derived from `alembic heads` — a brand-new
  migration passes every test of its own and fails this one with `assert '0011' == '0010'`,
  which reads like a chain bug rather than a one-line test update (hit adding 0011, 3.7 t1).
- **How to apply:** New head → grep `tests/test_migrations.py` for the previous head string.
  It is on no task brief's file list, so only a full run surfaces it.

## The PostGIS image installs extensions Alembic will then want to drop

- **Rule:** A fresh `postgis/postgis:16-3.4` volume ships `tiger_geocoder`, `topology` and
  `fuzzystrmatch`; the init script `docker/…/02-drop-image-extras.sql` removes them — keep it.
- **Why:** Those extensions bring their own tables, so `alembic check` on a raw dev DB
  reports a diff unrelated to our models and autogenerate proposes dropping them (`8c00270`).
- **How to apply:** If `alembic check` is suddenly dirty on a machine that just recreated
  its volumes, check for those schemas before suspecting the models.

## `sa.literal(value, JSONB)` inside `.bindparams()` binds the wrong object

- **Rule:** For a JSONB literal in a raw migration `sa.text(...)`, pre-serialize with
  `json.dumps` and bind it as text with an explicit `CAST(:x AS jsonb)` — never pass
  `sa.literal(value, postgresql.JSONB)` as the keyword value.
- **Why:** `.bindparams(key=sa.literal(v, type_))` binds the `BindParameter` construct
  itself, not `v`; asyncpg then raises `DataError: ... object has no attribute 'encode'`
  (migration 0010, caught RED/GREEN, never reached a database).
- **How to apply:** Prefer `op.bulk_insert` with `sa.column(..., postgresql.JSONB())` and a
  plain dict (0009's pattern, unaffected); otherwise `json.dumps` + `CAST`.

## A bind param immediately followed by `::` loses its last letter in `sa.text()`

- **Rule:** Never write `:name::cast_type` inside `sa.text(...)` — write
  `CAST(:name AS cast_type)`.
- **Why:** `TextClause`'s regex is `(?<![:\w\x5c]):(\w+)(?!:)`; the trailing lookahead makes
  `\w+` backtrack one character, registering the param as `valu` instead of `value`, so
  `.bindparams(value=...)` raises `ArgumentError: ... doesn't define a bound parameter named
  'value'` (migration 0012, 3.7 t2; caught RED/GREEN).
- **How to apply:** Grep any new raw-SQL migration or test for `:\w+::` before running it.

## A bare `alembic` CLI command targets the shared dev DB, not your worktree's test DB

- **Rule:** Never run `uv run alembic revision --autogenerate` or `upgrade head` bare in a
  worktree — export `DATABASE_URL="$DATABASE_URL_TEST"` for that one command first (mirror
  `tests/conftest.py::_migrated_test_db`).
- **Why:** `migrations/env.py::_resolve_url()` falls back to the shared dev DB whenever
  nothing overrides it, and a sibling worktree's not-yet-merged branch may have already
  advanced ITS `alembic_version` past revisions yours doesn't have — the bare command then
  fails `Can't locate revision identified by '0013'`, reading like local corruption, not a
  shared DB ahead of your files (hit building 3.8).
- **How to apply:** Before any manual Alembic CLI use, set the override. A revision error
  naming a version you don't have locally means check which DB you connected to first.

## Closing a deferred FK can break a DIFFERENT module's tests, invisibly

- **Rule:** After a migration adds `NOT VALID` + `VALIDATE CONSTRAINT` on a column another
  module already writes to, `grep -rn '<column>' tests/` across the WHOLE suite before
  reporting done — not just the tests your own file list names.
- **Why:** Migration 0015 closed `calculations.application_id`'s FK, absent since 3.7
  because `applications` didn't exist yet. `tests/modules/norms/test_calculations_api.py`
  inserted `Calculation(application_id=uuid.uuid4())` directly — a deliberately fabricated
  id, by its own docstring, since no real table existed to reference at the time — and it
  started raising `ForeignKeyViolationError`. `make heads` and the autogenerate-diff test
  saw nothing wrong; only a full `pytest -q` surfaced it (3.9a t1), in a file the task's own
  file list never mentioned.
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

## A service-level pre-check can leave its mirrored DB CHECK permanently unexercised

- **Rule:** When a service pre-checks a rule a CHECK also enforces (our defense-in-depth
  pattern), write a SEPARATE model-level test inserting through the ORM directly — an
  API-level test alone never reaches the CHECK.
- **Why:** `Contour.parent_needs_subcontour` had a test from Task 1, but only through
  `POST /gis/contours`, which raises `ERR-VAL-001` from the service before a row is even
  constructed; the CHECK first fired in Task 3's direct-ORM test.
- **How to apply:** Adding a `CheckConstraint` → grep for a `pytest.raises(IntegrityError)`
  that actually exercises it, not just the guard in front of it. Two tests, not one.

## `IntegrityError` IS a `DBAPIError` — never assume which one a DB failure raises

- **Rule:** `except IntegrityError` before `except DBAPIError`, never after, and never one
  clause inspecting `exc.orig` by hand. The same discipline applies to `pytest.raises(...)`:
  verify empirically which class a given failure raises, never assume from its category.
- **Why:** `gis.service.create_version` had only `except DBAPIError`, mapping every DB
  failure to `ERR-GIS-001` ("unreadable geometry"); a `uq_contour_version_no` race raises
  `IntegrityError`, a subclass, so a version-number conflict was reported as a geometry
  defect (3.6a t3). Task 7's bulk importer drives the same path — the bug was live.
- **The mirror, in a test:** an append-only trigger's plain `RAISE EXCEPTION` (audit_log,
  calculations, application_status_history) carries SQLSTATE `P0001`, outside the `23xxx`
  integrity-violation class its name suggests — it surfaces as `DBAPIError`, not
  `IntegrityError`. A 3.9a-applications-core plan's own verbatim
  `pytest.raises(IntegrityError, match="append-only")` against exactly this idiom could
  never pass; `tests/modules/audit/test_audit_log.py` and `tests/modules/norms/test_models.py`
  already assert `DBAPIError` for the identical trigger shape.
- **How to apply:** Before adding a second `except` beside an existing `DBAPIError`, OR
  writing `pytest.raises` against any DB failure — a trigger's RAISE included — check
  `__mro__` and verify empirically which exception a real run actually raises.

## Recovering from a failed insert to keep writing on the same session needs a SAVEPOINT and `exc.orig.__cause__`

- **Rule:** To catch one specific constraint's `IntegrityError` and still use the same
  session afterward (write evidence, commit), wrap the risky insert in `async with
  db.begin_nested():` (a SAVEPOINT), never a bare `db.rollback()`; identify WHICH
  constraint fired via `getattr(exc.orig.__cause__, "constraint_name", None)`, never
  `exc.orig` or the formatted message.
- **Why:** `signatures.service.sign()`'s race path writes an audit entry and commits after
  recovering. A bare `db.rollback()` undoes the WHOLE transaction, not just the failed
  statement — it silently discarded a caller's own earlier, uncommitted work on the same
  session, and the next INSERT then failed on an FK pointing at a just-un-inserted row.
  Separately, `exc.orig` is SQLAlchemy's asyncpg wrapper and exposes only `pgcode` (generic
  per SQLSTATE class — every unique violation is `23505` regardless of index);
  `exc.orig.__cause__` is asyncpg's OWN exception, which alone carries `constraint_name`.
- **How to apply:** `signatures.service.sign()`'s `try: async with db.begin_nested(): ...
  except IntegrityError:` block, compared against the ONE literal constraint name the
  branch cares about, is the template — a savepoint only when the caller keeps using `db`
  afterward; a bare `except IntegrityError: raise err(...)` needs none.

## A service meant as THE one write path for future callers locks its row, even with one caller today

- **Rule:** A function documented as "the ONE way" something gets written locks its row
  (`with_for_update=True`, **and `populate_existing=True` with it**) before checking and
  writing; a plain read stays lock-free.
- **Why:** `applications.service.set_status` read `Application` unlocked: two READ
  COMMITTED callers moving one row off `INVOICED` (a scheduler job, a payment callback)
  both passed validation and the second UPDATE silently overwrote the first (review C1)
  — the same shape `notifications.service._deliver`'s own `with_for_update` already fixed.
- **The lock alone is half the fix (3.9a review C2):** the loader populates only
  *unloaded* attributes of an instance the session already holds, and
  `expire_on_commit=False` never expires them, so a caller that ran `service.get(...)`
  first validates the STALE status under a correct lock. Now mechanical —
  `tests/test_code_conventions.py::test_every_locking_get_also_repopulates_the_row`.
- **How to apply:** Give the write path a locking repo read distinct from the plain one
  (`get_application_for_update`). A single session cannot prove a lock — open two via
  `make_session_factory(engine)` (`tests/modules/applications/test_public_surface.py`'s
  own two-session test is the template): `asyncio.create_task` + `not task.done()` while
  the first stays open, then commit and assert the second raises.

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

## The row in memory is not what Postgres stored

- **Rule:** After `flush()`, `await db.refresh(row)` before returning or serializing it,
  whenever the response reads a column the DB itself decides — an `onupdate=func.now()` after an
  UPDATE, or a caller-supplied value in a fixed-scale `NUMERIC` after an INSERT.
- **Why:** SQLAlchemy fetches `onupdate` via `RETURNING` on an INSERT but leaves it expired
  after a plain UPDATE — reading it outside the session's async context raises
  `MissingGreenlet` (`notifications.service.archive_template`, on `updated_at`). The INSERT
  side is the mirror: implicit `RETURNING` covers only what the DB generates
  (`server_default`, identity), so posting `"1.5"` into `Tariff.coefficient numeric(12,6)`
  left the in-memory row showing `"1.5"` while Postgres held `"1.500000"` (3.7 t3).
- **How to apply:** Refresh whenever a service both mutates/creates such a column AND returns
  that same row — an existing archive-path precedent without a read-back does not cover you.
- **On the way out:** a `Decimal` field backed by `NUMERIC(p,s)` also needs a
  `field_serializer` doing `format(value, "f").rstrip("0").rstrip(".")`
  (`gis.schemas._trim_decimal`) — `declared_area_ha` returns `Decimal('2.6000')` for a posted
  `"2.6"`. Never `Decimal.normalize()`: `Decimal('100.0000').normalize()` is `Decimal('1E+2')`.

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
  - **JSONB bind** — the engine sets no `json_serializer`, so a `Decimal` `{amount}` or a
    `date` `{due_date}` in `notifications.params` raises `TypeError: Object of type Decimal
    is not JSON serializable` at flush, INSIDE the caller's business transaction.
  - **`DomainError` details** — `app.main`'s handler renders with stock `JSONResponse`,
    unlike a `response_model` route; `ERR-GIS-003`'s raw `Decimal`/`UUID` raised inside the
    exception handler itself, turning a blocked publish's clean 422 into a 500 (3.6a t5).
  - **A parsed form body** — a multipart file part is an `UploadFile`, not a string, so the
    Eskiz callback's dead-letter payload 500'd on a one-line curl against the anonymous
    route that exists precisely to survive garbage.
- **How to apply:** `gis.checks.jsonable` is the template; form dicts get
  `{k: v if isinstance(v, str) else f"<{type(v).__name__}>" for k, v in form.items()}`.
  A coercer whose callers need IDENTICAL conversions belongs in ONE function — the gis pair
  had already diverged (the schemas copy had no `UUID` branch) when review caught it, and
  `norms.calculator` has since grown a third copy. Only deliberately-different copies are
  exempt: `_json_safe` for audit snapshots behaves differently per consumer on purpose.

---

# Permissions, roles, transitions

## Zone scoping is not a permission check — a read path needs both

- **Rule:** `require_permission(...)` answers "may this role do this at all"; `zone_filter`
  answers "on whose rows". Every endpoint returning territory-scoped data needs BOTH,
  including the small sibling endpoints.
- **Why:** `GET /admin/users/{id}/permissions` passed the permission gate but was not
  zone-scoped like the user card next to it, so a regional admin could read another region's
  user through it (3.3b final review, `aa1d551`).
- **How to apply:** Adding an endpoint next to a scoped one, copy its scoping, not just its
  permission code. Add a cross-zone denial test.

## A superuser bypass must be reflected in every path that REPORTS permissions

- **Rule:** `sys_admin` skips the check in `require_permission` (decision #41) — so every
  endpoint answering "what may I do" must special-case it too.
- **Why:** `GET /auth/me` reported an empty `permissions[]` for a superuser holding no
  personal grants: fully privileged in fact, powerless on screen, and the adminka would have
  hidden every button (3.3a — `MeOut.is_superuser` plus the full registry).
- **How to apply:** Any new "what can this user do" response gets the superuser branch, not
  just the enforcement point.

## A role name from spec or plan prose is never a `roles.code` — and `rahbar` maps to two

- **Rule:** Before seeding any role-based grant, read `0003_auth.py` for the actual
  `roles.code`. There is no `rahbar` code; **which code the word means is DISPUTED**, so
  a plan saying "the rahbar approves" is a question to settle, not a value to copy.
- **Why:** `plans/03.6a-gis-core.md` used `rahbar`; `INSERT … SELECT … WHERE code =
  'rahbar'` inserts zero rows silently, so `gis.contours.approve` reached nobody and every
  "the rahbar approves" test passed for the wrong reason (3.6a t1). The dispute (3.9a):
  `0003_auth.py:189` seeds `executor_head` = «Ваколатли шахс», the LESHOZ head, while
  `leadership` = «Агентлик раҳбарияти» has view+export only in `tz/03` — yet `tz/03` puts
  «Т» on «Заявка» for «Раҳбар». 3.6a/3.7 read it as `leadership`; 3.9a granted
  `applications.decide` to BOTH (fail-safe, revocable). Open in `tz/12`.
- **How to apply:** A grant for "the rahbar" → grant both codes and say why, or ask. Any
  grant → assert the exact `(role, permission)` set in a test
  (`test_applications_permission_seeds`): a wrong code inserts zero rows, never an error.

## A `_client_for` fixture's permission list must mirror the PRODUCTION role's grants

- **Rule:** When a migration grants a ROLE several codes, a fixture standing in for that role
  via `_client_for(db, ...)` must list ALL of them — that branch builds the user under
  `role_code="executor_staff"` with only the personal grants given, inheriting nothing from
  the real role's `role_permissions` row, whatever the fixture is named.
- **Why:** `leadership_client` (3.7 t4) was written with `NORMS_APPROVE, NORMS_MANAGE` under
  the docstring "approves, never publishes" — true of the default `norms_publish_scope=central`
  OUTCOME, wrong about the GRANT (0011 gives `leadership` `norms.publish` too; ruling 16's
  point is that the SETTING blocks it). Both brief-verbatim tests failed outright: the one
  asserting `ERR-ACL-002` got `ERR-ACL-001` — the route's dependency rejecting the request
  before the service's scope check ever ran.
- **How to apply:** Check what the role holds in its seeding migration, not the fixture's
  docstring — a docstring can describe the common-case OUTCOME while omitting a GRANT.

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
  `archive_versioned` has no `created_by` — archiving is single-actor — so the identical
  `require_any_permission` on `/archive` let any `TARIFFS_MANAGE` holder pull a published row
  out of force alone: worse than the original bug, a silent 200 instead of a loud wrong
  status. Fixed by keeping the route wide and adding an in-handler check that fires only when
  the row is `published` (`gis.service._may_manage_layers`'s two-branch shape). Before
  widening a gate, check every SIBLING route on it for its own escape hatch.

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

- **Rule:** When several endpoints each perform one step of a shared transition, a
  precondition belonging to the WHOLE transition — "is this even a valid target for this
  workflow" — lives in one function all of them call, never only in the step a well-behaved
  caller reaches first. And a transition whose loop moves zero child rows is refused, not
  advanced.
- **Why:** `submit_import_review` alone got the "refuse a non-contour batch" guard;
  `approve_import`/`publish_import` still gated on `row.status`. Since `CONTOURS_APPROVE` is a
  DIFFERENT permission from `CONTOURS_MANAGE`, a rahbar-only actor could call `/approve`
  directly on a freshly-parsed batch — never able to call submit-review at all — and both
  loops found zero rows and still advanced the status (3.6a t8; reproduced with the fix
  stashed, 200 instead of 409).
- **How to apply:** Factor the shared preamble — row lookup, zone check, validity check,
  status check — into one function. A loop finding nothing is not evidence that nothing
  needed to happen.
- **The same discipline on inputs:** before reporting a versioned-row creator done, walk
  every caller-settable field that is an FK or half of a period pair and confirm EACH has a
  service guard ahead of `flush()`. Task 4 guarded `contour_id`/`approval_doc_id` while
  `activity_type_id`, `geobotanic_doc_id` and `effective_to < effective_from` reached
  `flush()` as `ERR-SYS-001`/500 — and the same gap sat in the SHARED `create_versioned`
  (`POST /tariffs` with a garbage id was a 500 too). `add_classifier_item`'s
  `valid_to < valid_from` is the period template, `_assert_doc_active` the FK one — reuse the
  same helper per meaning, never a near-identical second copy.

## A gate that reads only ONE of the two things it guards is bundling two concerns

- **Rule:** When an `if` guards a block computing several values, check that EVERY value in
  the block reads something from the condition itself. A value computed without ever touching
  the condition's subject is gated on the wrong thing, even if it is correct today.
- **Why:** `calculator.calculate` computed `used_sb` (a REQUEST fact — `request.items` ×
  `coef_sb:<code>`) inside `if snapshot.norm is not None:` (a NORM fact). Task 7 needed the
  same number when no norm exists yet — a real, supported case — so a preview for a fresh
  contour silently returned `used_sb=None` instead of the missing-parameter error. The first
  fix added a SECOND resolution of the same lookup in the service: two places deciding a
  limit-relevant number, agreeing only by luck.
- **How to apply:** Split the gate at the root — `if request.activity_code == GRAZING:` for
  `used_sb`, `if snapshot.norm is not None:` for `max_sb`/`remaining_sb`. Before adding a
  caller-side workaround for a gap in a shared function, check whether the gate reads what it
  claims to gate on: a condition never referenced inside its own block is the tell.

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
- **How to apply:** Guard in the module's public entry point (`run_checks` is what 3.9 calls
  directly), not in each router's schema. `ERR-VAL-001` with `period_reversed`/`period_too_long`;
  the ceiling is a named constant carrying its domain reason (`MAX_PERIOD_DAYS` — ВМҚ 689
  redoes the geobotanical survey every five years). `from == to` must still pass.

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

---

# PostGIS

## `ST_Intersects` alone reports every shared border as an overlap

- **Rule:** A "do these overlap" predicate over real polygons is never plain
  `ST_Intersects`/`ST_Overlaps` — compute the intersection AREA and compare it to a named,
  configurable tolerance.
- **Why:** Two neighbouring published contours sharing a fence line are the NORMAL case on
  real cadastral data; `ST_Intersects` is `true` for a zero-area shared border exactly as for
  a genuine double-booking, so the spec's own `ST_Overlaps` check would have refused to
  publish valid neighbours (decision #24's correction).
- **How to apply:** `gis.checks._intersections` computes
  `ST_Area(ST_Intersection(a,b)::geography)` against `gis_overlap_tolerance_m2` (default
  100 m²), proven by `test_checks.py`'s `draft_version_touching_it` vs
  `draft_version_overlapping_it`. Norms' own territory checks are the next candidate.

## An empty layer makes a containment check meaningless — decide what "no data" means first

- **Rule:** A topology/containment check against a layer that might still be empty needs a
  THIRD outcome — `skipped`, never `pass` or `fail` — decided at design time.
- **Why:** `gis.checks._within_fund` would report every contour as `outside_forest_fund` — a
  hard fail blocking every publication — for as long as the Agency's fund-boundary delivery
  is pending (plan ruling 9). Without the `skipped`/`layer_empty` branch (straight from
  `count(*) == 0` in the same query) not one contour could have published this month; the
  check turns itself on the day the data lands, with no code change.
- **How to apply:** Any check whose reference set is a layer this project does not yet control
  the population of gets an explicit empty-set branch, returned as its own named result.

---

# HTTP layer

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

## A cap checked after reading the body is not a cap — and an anonymous route must cap what it PERSISTS

- **Rule:** Enforce a size limit from `Content-Length`/`UploadFile.size` FIRST, then
  chunked-read with a running total. Separately, any column an unauthenticated caller can
  fill gets an explicit size cap with a truncation marker.
- **Why:** The 3.3b upload path read the whole body into RAM before comparing it to
  `max_upload_mb`: enforced, but the memory was already spent (`aa1d551`). And
  `inbound_dead_letters.payload` stored an arbitrary-size body from the anonymous Eskiz
  callback while `error` beside it was cut to 1000 chars — there is no body-size middleware
  and no purge job for dead letters. "Every other JSON endpoint does the same" was the wrong
  defence: the others do not PERSIST the body.
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
  breaker for the same outcome, holding back every other message on that destination. The
  mirror bit harder (3.5 final review): one helper answered both "is this recipient reachable"
  (permanent) and "is the ops kill switch on" (temporary), so flipping
  `notifications_sms_enabled` off for an hour DESTROYED every queued SMS with a reason blaming
  the recipient — unrecoverably, since admin requeue only works on `dead` rows.
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

## Never echo an outbound payload into a raised exception

- **Rule:** A sender may report the transport failure (status code, provider error code) —
  never the message body it was trying to send.
- **Why:** `outbox_messages.last_error` is admin-visible via `/admin/integrations/*` AND
  logged, so a sender formatting the payload into its exception leaks live OTP codes to
  anyone holding the admin outbox permission.
- **How to apply:** Every new sender raises with transport metadata only, plus a test
  asserting the code is NOT in `str(exc)`.

## Never ask a provider for a callback you cannot correlate

- **Rule:** Only request a delivery report for a send that has a stored row to correlate it
  against; pass an explicit "no callback" flag otherwise.
- **Why:** `EskizSmsSender` put `callback_url` in every payload while `RealOtpSender` passed a
  throwaway uuid as the reference, so at `sms_mode=real` EVERY OTP would produce an
  `inbound_dead_letters` row holding the recipient's phone number, forever — no purge job
  covers dead letters and the DLQ's triage purpose would drown (3.5 final review).
- **How to apply:** Wiring a provider callback, ask what the DLQ does with a report matching
  nothing — and remove the cause rather than filtering it.

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

## The test DB is shared, persistent, and never empty — including the spot you picked

- **Rule:** A test may only touch rows it created. No unscoped `UPDATE`/`DELETE`, no assuming
  an empty database, and no assuming an empty *neighbourhood*.
- **Why:** The DB is shared across worktrees and runs, committing client fixtures leave rows
  behind forever, and the round-trip test wipes it wholesale (collection order pinned in
  `tests/conftest.py`). Four consequences paid for already, each with its remedy:
  - **A fixed literal accumulates:** `box_wkt(69.9, 41.5)` held 4 stray contours before
    3.6a t4 and 11 after, so a "nothing overlaps here" assertion there is flaky from birth
    → randomise (`random_box_wkt()`, `unique_suffix`, a `storage_key`), unless a sibling
    deliberately needs proximity (`neighbouring_published_contour`).
  - **A claim-the-oldest worker takes a stranger's row:** `process_pending` claims the
    oldest `pending` import in the DB, not yours, and an interrupted run strands one forever
    (`test_two_workers…` sees `[1, 1]` not `[0, 1]`) → a package-scoped autouse drain that
    runs the JOB, bounded by `DRAIN_LIMIT`, never an unscoped DELETE.
  - **Paging:** page 1 is full of previous runs, so
    `test_an_applicant_sees_published_contours_only` went red the moment the endpoint was
    paged → assert `total` plus a scoped filter (a fresh `organization_id`), not membership.
  - **A refused action leaves its row:** `test_a_maker_cannot_archive_a_published_tariff`
    succeeds BY being refused, so its `science` tariff stays published forever — breaking
    `test_science_has_no_tariff` (`count(*) == 0`; archived counts too) and its own next run
    with `period_overlap` → a yield-fixture teardown with a scoped DELETE.
- **How to apply:** Scope every assertion by the ids your fixture created. Run any new
  negative test twice in a row, and as part of the FULL suite — this class is invisible in
  isolation. Do not reorder the conftest collection hook.

## A `_client_for` client's setup-time commit only covers fixtures listed before it

- **Rule:** Every client fixture built over `_client_for` registers an httpx `request` event
  hook re-committing `db` before each outgoing call.
- **Why:** pytest instantiates fixtures in the LEFT-TO-RIGHT order of the parameter list
  (verified empirically), so in `test_x(gis_client, leshoz, contours_layer)` the client's
  internal commit runs before `leshoz` even executes — `leshoz`'s `flush()`-only row stays
  invisible to the app's separate connection and the test FK-fails (confirmed with an
  independent asyncpg connection finding nothing in `organizations`).
- **How to apply:** Copy `tests/modules/gis/conftest.py`'s
  `_commit_pending_before_requests` for any new signed-in-client fixture that will ever be
  combined with a write fixture; never rely on parameter order.

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

## Seeded reference data with future effective dates is a scheduled test failure

- **Rule:** A test asserting an exact money/norm figure computed from `business_today()` must
  FREEZE the date — patching it in the CALLING module's namespace
  (`app.modules.norms.service.business_today`), never in `app.core.time` — pinned inside the
  window of the row it means to exercise.
- **Why:** `0012` seeds `bhm` as two dated rows (412 000 until 2026-08-31, 440 000 from
  2026-09-01). 3.7 t7's tests hard-coded amounts derived from 412 000 while `_compute`
  resolved `on_date=business_today()`, so the suite was green the day it was written and would
  have gone red on 1 September with nobody touching the repository.
- **How to apply:** Adding a seed row whose period starts in the future, grep the suite for
  the currently-in-force figure and for `business_today`. Never "fix" such a test by
  recomputing the expectation from whatever row is in force — that passes against a WRONG
  tariff, the opposite of what the test is for.

## A re-exported fixture shadowed by a same-file parameter trips ruff's F811

- **Rule:** When a conftest imports another module's fixture ONLY to re-export it and ALSO
  uses that name as a parameter on a fixture defined in the SAME file, import it as
  `from module import name as name` — never rename the parameter, which would break pytest's
  name-based injection.
- **Why:** Pyflakes flags a parameter shadowing an "unused" import as F811, even though the
  identical shape is silent when the earlier binding is a locally-DEFINED fixture:
  `tests/modules/gis/conftest.py`'s own `published_contour(db, contours_layer, leshoz,
  approval_doc)` never trips it, while `tests/modules/norms/conftest.py` re-exporting those
  four and using them as parameters failed `ruff check` on all four (3.7 t1).
- **How to apply:** Keep `# noqa: F401` for names you only re-export; use `as <same name>` for
  the ones you also consume locally. `ruff check --fix` will split them into their own
  `from ... import (...)` — let it.

## `dict(rows.all())` on a raw `text()` query passes at runtime, fails pyright

- **Rule:** Build a dict from a raw-SQL `Result` with a comprehension —
  `{row[0]: row[1] for row in rows.all()}` — never `dict(rows.all())`.
- **Why:** A `text()` query's rows are `Row[Any]`; pyright cannot confirm the 2-tuple arity,
  matches the wrong `dict()` overload (`Iterable[list[bytes]]`) and reports `reportCallIssue`
  on code that runs correctly (migration 0012 tests, 3.7 t2). The identical call over a TYPED
  `select(Col.a, Col.b)` does not trip it — SQLAlchemy infers the arity statically there.
- **How to apply:** Comprehension for raw SQL; reserve `dict(rows.all())` for a typed
  `select(...)`.

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

## A module's first HTTP-driven test file needs its own `_app_on_test_db` guard

- **Rule:** The first test file in a module that drives requests through `create_app()`
  (not direct `service.py` calls) must add an autouse fixture monkeypatching `DATABASE_URL`
  to `database_url_test` plus `get_settings.cache_clear()` — copy it from any other
  HTTP-tested module's `conftest.py`, never assume it is inherited.
- **Why:** Without it, `create_app()`'s lifespan opens the shared dev `DATABASE_URL`, not
  the test one — every session cookie a fixture wrote is invisible to it, and EVERY request
  401s (`ERR-AUTH-002`), reading as a blanket auth failure with no hint that the database is
  the actual bug (hit on `signatures/test_api.py`, 3.8 t7).
- **How to apply:** Adding a module's first `router.py` test file, grep its own
  `conftest.py` for `_app_on_test_db` before writing a single `client.get(...)`; add it
  there (autouse) if missing, rather than per-file.

## An unannotated test fixture parameter hides a `str | None` argument-type error pyright would catch

- **Rule:** Annotate test function parameters with their real fixture type (`a_user: User`,
  not bare `a_user`) — pyright then checks attribute access against the actual model,
  catching a nullable-column mismatch an unannotated (implicit `Any`) parameter silently
  swallows.
- **Why:** `tests/modules/signatures/test_sign.py` calls `_pkcs7(a_user.pinfl)` (a
  `str`-only parameter) with zero pyright errors ONLY because its test functions never type
  `a_user` — `User.pinfl: Mapped[str | None]` needs narrowing. Adding `a_user: User` in a
  new file surfaced three real `reportArgumentType` errors for the identical expression
  (3.8 t5).
- **How to apply:** Prefer typed test parameters generally; narrow a fixture's nullable
  attribute explicitly at the call site (`assert a_user.pinfl is not None`) instead of
  leaving the parameter unannotated to dodge the check.

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
