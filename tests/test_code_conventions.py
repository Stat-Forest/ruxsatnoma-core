"""Greppable conventions from `.claude/lessons.md`, asserted by CI instead of
by a reviewer — the codebase's stated preference (a mechanical check beats a
lesson beats an instinct; `.claude/skills/writing-lessons`, step 1).

Four rules live here so far. The first two came from the 3.9a final review:

- `date.today()` must not appear in `app/` — every calendar-day decision goes
  through `app.core.time.business_today()` (backend/CLAUDE.md "Time"). This is
  also the only place `app/core/numbers.py`'s `on_date` contract can be
  enforced (finding I3): the parameter takes any `date`, and its callers live
  in later stages.
- a locking `db.get(..., with_for_update=True)` must also pass
  `populate_existing=True` (finding C2).

and the other two from 3.11a:

- every integer QUERY parameter carries an upper bound, or it reaches asyncpg
  as `DataError: value out of int64 range` — a 500 anybody can type (t5).
- `register_event_subscriptions()` is idempotent for every registry it fills,
  not only for the event bus the autouse fixture restores (pre-flight P4).
"""

import ast
import pathlib

# `app/core/time.py` is where the rule is implemented and explained.
_EXEMPT = {pathlib.Path("app/core/time.py")}


def _today_calls(tree: ast.AST) -> list[int]:
    """Line numbers of `date.today()` / `datetime.date.today()` calls.

    An AST walk, not a text grep: `auth/service.py` and `auth/deps.py` both
    mention `date.today()` in a comment saying not to use it, and a text
    search cannot tell that apart from a call.
    """
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "today":
            continue
        target = func.value
        if isinstance(target, ast.Name) and target.id == "date":
            hits.append(node.lineno)
        elif (
            isinstance(target, ast.Attribute)
            and target.attr == "date"
            and isinstance(target.value, ast.Name)
            and target.value.id == "datetime"
        ):
            hits.append(node.lineno)
    return hits


def _sources() -> list[pathlib.Path]:
    files = sorted(pathlib.Path("app").rglob("*.py"))
    assert files, "no sources found — did the tests run from the repo root?"
    return files


def test_no_module_under_app_calls_date_today() -> None:
    offenders = []
    for path in _sources():
        if path in _EXEMPT:
            continue
        for line in _today_calls(ast.parse(path.read_text())):
            offenders.append(f"{path}:{line}")
    assert not offenders, (
        "use app.core.time.business_today() instead of date.today(): " + ", ".join(offenders)
    )


def _locking_gets_without_populate_existing(tree: ast.AST) -> list[int]:
    """Line numbers of `*.get(..., with_for_update=True)` calls that do not also
    pass `populate_existing=True`.

    Scoped to `Session.get` deliberately: it fetches ONE row by primary key, so
    the session plausibly already holds it, and the loader then refreshes only
    the attributes that are unloaded — the lock is taken over a stale copy. A
    `select(...).with_for_update(skip_locked=True)` queue poll
    (`integrations.repo`, `gis.repo`) claims a row it has never seen and is a
    different shape, so it is not covered here.
    """
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "get"):
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        if "with_for_update" in kwargs and "populate_existing" not in kwargs:
            hits.append(node.lineno)
    return hits


def test_every_locking_get_also_repopulates_the_row() -> None:
    """Final review C2. `with_for_update=True` does emit a real
    `SELECT ... FOR UPDATE` — it skips `Session.get`'s identity-map shortcut —
    but without `populate_existing=True` an instance already in the identity map
    keeps its cached column values, and `app/db.py`'s `expire_on_commit=False`
    never expires them. `applications.service.set_status` validated a transition
    against a status another session had already committed away, wrote a history
    row for a transition that never happened, and overwrote the committed value;
    `app/core/idempotency.py` had documented the same trap two stages earlier.
    The pairing is mechanical, so it is checked rather than remembered."""
    offenders = []
    for path in _sources():
        for line in _locking_gets_without_populate_existing(ast.parse(path.read_text())):
            offenders.append(f"{path}:{line}")
    assert not offenders, "a locking get() must also pass populate_existing=True: " + ", ".join(
        offenders
    )


def _integer_schemas(schema: dict) -> list[dict]:
    """Every `{"type": "integer"}` inside one parameter's JSON schema.

    A parameter typed `int | None` is not `{"type": "integer"}` but an `anyOf`
    over integer and null, so a flat read misses exactly the optional parameters
    a query string most often carries.
    """
    found = [schema] if schema.get("type") == "integer" else []
    for key in ("anyOf", "oneOf", "allOf"):
        for member in schema.get(key, []):
            found.extend(_integer_schemas(member))
    return found


def test_every_integer_query_parameter_carries_an_upper_bound() -> None:
    """3.11a t5. An `int` query parameter is bound into SQL as a bigint, and a
    value past that range reaches asyncpg as `DataError: value out of int64
    range` — an unhandled 500 for a query string anybody can type. It was found
    on `?number=` of `GET /public/permits/check`, the one anonymous route whose
    whole contract is that garbage produces an answer rather than a stack trace,
    and the same hole was open on every `?page=`/`?offset=` in the app (`page` is
    multiplied by `page_size` into an OFFSET, so it overflows sooner).

    Read off the OpenAPI schema rather than `app.routes`: since FastAPI 0.141
    `include_router` nests an `_IncludedRouter` instead of flattening `APIRoute`s
    into `app.routes`, so the obvious walk matches nothing and the check passes
    by examining zero parameters — which is how the first version of this test
    stayed green with the bound deliberately removed.
    """
    import os

    os.environ.setdefault("WORKERS_MODE", "off")
    from app.main import create_app

    schema = create_app().openapi()
    checked, offenders = 0, []
    for path, operations in schema["paths"].items():
        for operation in operations.values():
            for parameter in operation.get("parameters", []):
                if parameter.get("in") != "query":
                    continue
                integers = _integer_schemas(parameter.get("schema", {}))
                if not integers:
                    continue
                checked += 1
                if not all("maximum" in one or "exclusiveMaximum" in one for one in integers):
                    offenders.append(f"{path}?{parameter['name']}")
    # The negative control the first version lacked: a walk that matches nothing
    # cannot fail, so the count itself is asserted.
    assert checked >= 20, f"only {checked} integer query parameters seen — the walk is wrong"
    assert not offenders, (
        "an int query parameter needs an upper bound (Query(le=...), PAGING_MAX for "
        "paging) — unbounded, it reaches asyncpg as out of int64 range: " + ", ".join(offenders)
    )


def _registry_sizes() -> dict[str, int]:
    """The length of every module-level `list`/`dict` in an imported `app.*` module.

    Deliberately not restricted to the registries anyone has thought of: the
    point of the check below is to catch the registry NOBODY has thought of yet.
    A constant that is never mutated simply reports the same size twice and
    costs nothing.
    """
    import sys

    sizes: dict[str, int] = {}
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("app."):
            continue
        for attr, value in list(vars(module).items()):
            if attr.startswith("__") or not isinstance(value, list | dict):
                continue
            sizes[f"{name}.{attr}"] = len(value)
    return sizes


def test_registering_the_event_subscriptions_twice_registers_nothing_twice() -> None:
    """3.11a P4. `register_event_subscriptions()` fills registries that live
    OUTSIDE the event bus — `gis.service.OCCUPANCY_PROVIDERS` and
    `norms.service.LOAD_PROVIDERS` since 3.11a — and
    `tests/conftest.py::_isolate_subscriptions` is autouse, calls that function
    for EVERY test, and snapshots `app.core.events._SUBSCRIBERS` **and nothing
    else**. A registration that appends without checking membership therefore
    adds one copy per test: a provider's answer silently doubles, then triples,
    the failure lands in whichever file happens to run late, reads as test
    pollution rather than as a registration bug, and passes when that file is
    run alone.

    Generic on purpose. `permits/test_providers.py` pins today's two providers
    by name, which cannot see the THIRD registry a later module adds — and that
    is the one whose author will not have read this comment.
    """
    from app.event_subscriptions import register_event_subscriptions

    register_event_subscriptions()
    before = _registry_sizes()
    register_event_subscriptions()
    after = _registry_sizes()

    grown = {
        key: (size, after[key])
        for key, size in before.items()
        if key in after and after[key] != size
    }
    # A walk that examines nothing cannot fail (the lesson the integer-bound
    # check above paid for), so the population is asserted too.
    assert len(before) >= 20, f"only {len(before)} registries seen — the walk is wrong"
    assert not grown, (
        "registering the event subscriptions twice grew a registry — append only after a "
        "membership check, the way core.events.subscribe dedups its own pair: " + repr(grown)
    )


def test_ri_10_permit_statuses_are_a_subset_of_the_real_permit_statuses() -> None:
    """3.10b task 8/10. `payments.repo._RI_10_PERMIT_STATUSES` is a tuple of
    THREE LITERAL STRINGS, deliberately — `design/01` rule 3 forbids one
    level-4 module (`payments`) importing another (`permits`), so
    `payments/repo.py` cannot derive them from `permits.models.
    PERMIT_STATUSES` the way it derives its OWN enum-ish constants from its
    OWN model tuples. A TEST carries no such boundary and may import
    `permits.models` freely, which is exactly what this check does: without
    it, renaming `"active"` to something else in `permits.models.
    PERMIT_STATUSES` would silently strand the literal in `payments/repo.py`
    at its old spelling, and RI-10 — `tz/10`'s CRITICAL indicator for a
    reversed payment against a permit that already exists — would stop
    firing for every permit in that (renamed) status, with nothing red
    anywhere to say so.
    """
    from app.modules.payments.repo import _RI_10_PERMIT_STATUSES
    from app.modules.permits.models import PERMIT_STATUSES

    offenders = sorted(set(_RI_10_PERMIT_STATUSES) - set(PERMIT_STATUSES))
    assert not offenders, (
        "payments.repo._RI_10_PERMIT_STATUSES has a status permits.models."
        f"PERMIT_STATUSES no longer knows: {offenders}"
    )


def test_the_payment_confirmed_bus_name_is_written_down_exactly_once() -> None:
    """The 3.10a/3.11a seam. `payments` publishes on the bus name
    `payment_confirmed` and `permits` subscribes to it, and the two may not
    import each other (design/01 rule 3) — so the name is matched by STRING and
    a rename that misses one side breaks the seam in complete silence:
    `core.events.publish` on a name nothing subscribes to is a legal no-op, so
    the assigned executor simply stops being told a permit is due, on every
    payment, with nothing failing anywhere.

    `app/event_subscriptions.py` re-declared the literal until 2026-09-03 and
    `tests/modules/permits/test_issue.py` published a third copy of it, while
    `tests/modules/payments/test_end_to_end.py` subscribed through the constant
    — so publish and subscribe could genuinely part company with a green suite.
    Both now read `app.event_subscriptions.PAYMENT_CONFIRMED` (the seam's own
    re-export of the publisher's constant) or the publisher's constant itself.

    The check is textual because that is the failure: the SEAM is allowed to
    know both sides, and the identity assertion below would pass just as well
    with three literals that happen to agree today.
    """
    from app.event_subscriptions import PAYMENT_CONFIRMED
    from app.modules.payments.events import PAYMENT_CONFIRMED as PUBLISHED_NAME

    assert PAYMENT_CONFIRMED is PUBLISHED_NAME

    needle = f'"{PUBLISHED_NAME}"'
    owner = pathlib.Path("app/modules/payments/events.py")
    searched = sorted([*pathlib.Path("app").rglob("*.py"), *pathlib.Path("tests").rglob("*.py")])
    assert len(searched) >= 100, f"only {len(searched)} files walked — the walk is wrong"
    offenders = [
        str(path)
        for path in searched
        if path != owner and needle in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{needle} is written down outside {owner} — import "
        "`app.modules.payments.events.PAYMENT_CONFIRMED` (or the seam's re-export, "
        f"`app.event_subscriptions.PAYMENT_CONFIRMED`) instead: {offenders}"
    )
