"""Greppable conventions from `.claude/lessons.md`, asserted by CI instead of
by a reviewer — the codebase's stated preference (a mechanical check beats a
lesson beats an instinct; `.claude/skills/writing-lessons`, step 1).

Two rules live here so far, both from the 3.9a final review:

- `date.today()` must not appear in `app/` — every calendar-day decision goes
  through `app.core.time.business_today()` (backend/CLAUDE.md "Time"). This is
  also the only place `app/core/numbers.py`'s `on_date` contract can be
  enforced (finding I3): the parameter takes any `date`, and its callers live
  in later stages.
- a locking `db.get(..., with_for_update=True)` must also pass
  `populate_existing=True` (finding C2).
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
