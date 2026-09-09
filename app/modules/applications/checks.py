"""Which checks an application needs, run in one place (plan 03.9a task 4).

`run_all` is that place. Both the pre-check (task 4) and the submission (task
5) call it, and the whole difference between them is what a BLOCKING result
MEANS: data in the pre-check's response, an HTTP error at submission
(`first_blocking_error`, used by the submission and never by the pre-check).
Two lists of checks would be two answers to "may this be granted", and the one
the applicant saw would not be the one the reviewer acts on.

**Ruling 21 is the vocabulary**, and it corrects `design/02`'s own list:

    gis.checks     validity     -> gis_validity        blocking
                   within_fund  -> gis_within_fund     blocking
                   overlap      -> gis_overlap         blocking
                   restrictions -> DROPPED, not recorded
    norms.checks   norm         -> norm_available      blocking
                   season       -> norm_season         blocking
                   rotation     -> norm_rotation       blocking
                   fire_ban     -> norm_fire_ban       blocking
                   restrictions -> norm_restrictions   warning only
                   limit        -> norm_limit          blocking

`gis`'s own `restrictions` result is dropped because it is a strictly weaker
duplicate of `norm_restrictions` over the same three layers:
`gis.checks._restrictions` probes a single `on_date` and can never fail, while
`norms.checks._territory_checks` probes the WHOLE requested period and carries
the blocking decision (decision #49 ruling 16 put that call in `norms`). `vet`
and `cadastre` are 3.9b's and must not appear here.

Every row is written with `source='auto'`, and a repeat check is a NEW row —
never an update (ruling 12). The history is the evidence: a reviewer has to be
able to see that the contour passed at submission even if it would fail today.

Module boundary: `gis` and `norms` are reached only through their `service`,
reference data only through `admin.repo` (CLAUDE.md). Nothing here imports
`applications.service` — the dependency runs the other way.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, err
from app.modules.admin import repo as admin_repo
from app.modules.applications import repo
from app.modules.applications.models import Application, ApplicationCheck
from app.modules.auth import service as auth_service
from app.modules.gis import service as gis_service
from app.modules.norms import service as norms_service
from app.modules.norms.schemas import CalculationIn, LivestockItemIn

# `application_checks.source` — 3.9a is always the automatic run; 3.9b's manual
# fallback is what `manual_fallback`/`external_api` exist for (models.py).
SOURCE_AUTO = "auto"

# The activity whose load is a HERD rather than a declared quantity — the one
# place this module needs to tell the two apart, to know which field being empty
# makes an application unpriceable. Re-declared rather than imported from
# `norms.calculator.GRAZING`: `applications` reaches `norms` through
# `norms.service` only, and a calculator constant is not part of that surface.
GRAZING_ACTIVITY_CODE = "grazing"

# ruling 21's first table, as two maps from what the check modules emit onto
# `models.CHECK_TYPES`. A name absent from a map is deliberately dropped and
# never recorded — today that is `gis`'s own `restrictions` and nothing else.
GIS_CHECK_TYPES = {
    "validity": "gis_validity",
    "within_fund": "gis_within_fund",
    "overlap": "gis_overlap",
}
NORM_CHECK_TYPES = {
    "norm": "norm_available",
    "season": "norm_season",
    "rotation": "norm_rotation",
    "fire_ban": "norm_fire_ban",
    "restrictions": "norm_restrictions",
    "limit": "norm_limit",
}

# ruling 21's second table: which `ERR-` code a failing check refuses with.
# `ERR-GIS-005` is the only 409 among them — an overlap with another published
# contour is a conflict of state, not a malformed request.
_ERROR_BY_CHECK_TYPE = {
    "gis_validity": "ERR-GIS-001",
    "gis_within_fund": "ERR-GIS-002",
    "gis_overlap": "ERR-GIS-005",
    "norm_available": "ERR-NORM-001",
    "norm_limit": "ERR-NORM-002",
    "norm_season": "ERR-NORM-003",
    "norm_rotation": "ERR-NORM-003",
    "norm_fire_ban": "ERR-NORM-006",
}

# Everything ruling 21 marks blocking — DERIVED from the table above, never a
# second hand-maintained list of the same eight keys (review I2). Having an
# error code IS what blocking means here, and `first_blocking_error` indexes
# that table right after testing membership of this set: two lists that drift by
# one key turn a clean submission refusal into an unhandled `KeyError`, a 500 on
# the one path whose whole job is to refuse cleanly.
#
# `norm_restrictions` is therefore the ONE advisory type, by having no code: an
# intersection with a restriction or protection layer limits grazing, it does
# not forbid it, and the judgement is the reviewer's (3.9b). Moving a type in or
# out is a product decision, not a refactor — the same sentence `gis.checks` and
# `norms.checks` both carry — and it is now made in exactly one place.
BLOCKING = frozenset(_ERROR_BY_CHECK_TYPE)

# What `norms` needs before it can be asked anything at all
# (`norms.service._build_request_and_snapshot`). The same four fields task 5's
# completeness check requires, minus `applicant_id`, which is NOT NULL on the
# table and therefore cannot be missing.
REQUIRED_FOR_PRICING = ("activity_type_id", "contour_id", "period_from", "period_to")


def _jsonable(value: Any) -> Any:
    """A check's `details` as a JSONB column and a `DomainError` response can
    both take it: `Decimal`/`date`/`datetime`/`uuid.UUID` -> `str`, recursing
    through `dict`/`list`/`tuple`.

    Not optional, and not decoration. Nothing in this app configures a JSON
    encoder (lesson), and `gis.checks._intersections` puts a raw `Decimal`
    (`area_m2`) and a raw `uuid.UUID` (`feature_id`) straight into a result's
    `details` — either one reaches `json.dumps` unconverted and raises
    `TypeError` inside the JSONB bind, or inside `app.main`'s own exception
    handler, turning a clean refusal into a 500.

    `Decimal` -> `str`, never `float`: these details carry conditional-head
    counts and remaining limits, and `str(Decimal(...))` round-trips exactly
    where a float would not.

    **Named `_jsonable` after `gis.checks.jsonable` and
    `norms.calculator.jsonable`, which are its templates and which it copies
    rather than imports because the module boundary forbids reaching into
    either** (review I1). It is deliberately NOT the `_json_safe` that
    `service.py` keeps beside it: that one is non-recursive and renders a
    `Decimal` through `format(value, "f")` for an audit snapshot of the
    application's own scalar columns, so the two disagree on an exponent-form
    value (`format(Decimal("1E+2"), "f")` is `"100"`, `str(...)` is `"1E+2"`).
    Two same-named private helpers in one module would invite a future reader
    to move a call from one to the other; the different name is the warning.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


async def _applicant_address_missing(db: AsyncSession, applicant_id: uuid.UUID) -> bool:
    """Ruling #113: `applicants.address` blank counts as missing, exactly like
    a NULL application column above it — a citizen may register and look
    around with no address on file (OneID does not always supply one), but a
    submission needs one for requisite 11 of form 1-ilova.

    Read through `auth.service`, never `auth.repo` directly (CLAUDE.md module
    boundary — reference/owning-module data crosses only through the other
    module's service). `applicant_id` is NOT NULL on `applications`, so
    `get_applicant` returning `None` here would mean the FK itself is broken;
    treated as missing rather than asserted, because this function's contract
    is "what is not fillable yet", not "prove the database is healthy".
    """
    applicant = await auth_service.get_applicant(db, applicant_id)
    return applicant is None or not (applicant.address or "").strip()


async def missing_for_pricing(db: AsyncSession, application: Application) -> list[str]:
    """Which of the fields `norms` needs are still empty — `[]` when the draft
    can be priced and checked.

    ONE definition, three readers: `run_all` decides between real norm checks
    and `skipped` rows by it, `calculation_payload` promises its own caller has
    consulted it, and `service.precheck` decides whether there is a price to
    report at all (lesson: a precondition shared by several steps belongs in
    one function every step calls).

    A draft is autosaved field by field (ruling 7), so a half-empty one is the
    NORMAL input here, not an edge case. Answering it with a 500, or with an
    empty check list, would both be wrong: the applicant is told which field is
    still missing, in the `details` of a `skipped` row — the idiom both check
    modules already use (`gis.checks._within_fund`, `norms.checks`), and the
    reason `application_checks.result` carries `skipped` at all.

    The items/quantity half is not decoration: without it `norms.calculator`
    raises `ERR-VAL-001 {"reason": "quantity_required"}` for a haymaking draft
    whose quantity is not filled in yet, and the applicant would meet a
    validation error from a module they never called instead of "you left a
    field empty". It is the same rule task 5's completeness check applies
    before it refuses a submission.

    **`quantity` is required for EVERY non-grazing activity, including a
    tariff-exempt one, and that is deliberately stricter than `norms` itself**
    (review, minor 3). `norms.calculator` only raises `quantity_required` when
    a tariff row exists; for `science` — the one activity VMQ 278 genuinely
    leaves un-rated (`tariff_exempt:science`, migration 0013) — it bills zero
    and never reads the field. Three reasons the gate stays wider all the same:

      * `applications.quantity` is a REQUISITE of the printed permit
        (`tz/13` 1-ilova: the document states how much of what, in the
        activity's own unit — `ha` for `science`), not merely an input to a
        price. A permit that prints no amount is not a permit.
      * Task 5's completeness check requires it for every non-grazing activity.
        A pre-check that reported "ready to file" and a submission that then
        refused with `ERR-APP-001` would make this route useless at the one
        thing it exists for: predicting the submission.
      * Narrowing it would mean asking `norms` whether an activity is
        tariff-exempt, which its public surface does not expose — and adding an
        entry point to another module so that a citizen may leave a field of
        the permit blank is the wrong trade.

    So a scientific-research filing sends `quantity` — the area it will work on
    — like every other non-grazing one, and is then priced at zero.
    `test_precheck.py::test_a_tariff_exempt_activity_still_has_to_declare_its_
    quantity` pins both halves.

    **`address` (ruling #113, `tz/12` #20) joins this list too, though it
    prices nothing.** It is a requisite of the PRINTED document, exactly the
    reasoning `quantity` above already carries — and the name on this
    function no longer describes only what `norms` needs, it describes what
    "ready to submit" means, which is what both of this function's callers
    have always actually used it for. Checked UNCONDITIONALLY, before either
    early return below: an application whose activity is not chosen yet is
    still missing an address, and reporting only one of the two would make a
    second `PATCH`-then-precheck round trip necessary to learn about the
    other.
    """
    missing = [name for name in REQUIRED_FOR_PRICING if getattr(application, name) is None]
    if await _applicant_address_missing(db, application.applicant_id):
        missing.append("address")
    if application.activity_type_id is None:
        return missing
    activity = await admin_repo.get_activity_type(db, application.activity_type_id)
    if activity is None:
        # Not reachable through `PATCH` (`service._assert_references` refuses an
        # unknown activity) — reported as missing rather than raised, because
        # this function's contract is "what is not fillable yet", and the row it
        # produces names the field either way.
        return [*missing, "activity_type_id"]
    if activity.code == GRAZING_ACTIVITY_CODE:
        if not await repo.list_items(db, application.id):
            missing.append("items")
    elif application.quantity is None:
        missing.append("quantity")
    return missing


async def calculation_payload(db: AsyncSession, application: Application) -> CalculationIn:
    """The `norms` request this application describes — what `preview`,
    `run_checks` and (task 5) `save_calculation` are all asked with, built once
    so the checks an applicant sees and the price they are quoted can never
    describe two different requests.

    The caller must have confirmed `missing_for_pricing` is empty first; the
    asserts below are that contract, not input validation.

    Two translations happen here and nowhere else. `application_items` stores a
    `livestock_type_id` while `norms` speaks the livestock CODE, and
    `applications.benefit_category_item_id` is a `classifier_items` id while
    `norms` speaks the benefit CODE — both resolved through `admin.repo`,
    because reference data is never re-queried from another module's tables
    (CLAUDE.md).

    `application_id` is left at its default. Task 5 widened
    `norms.schemas.CalculationIn.application_id` to a real `uuid.UUID | None`,
    but it stays unset HERE: the pre-check prices without storing anything
    (ruling 19), and the one caller that does store — `service.submit` at step
    9 — adds the binding itself with `model_copy(update=...)` on the very
    object this function returned, so the row that is stored and the package
    that was signed describe one request rather than two builds of it.
    """
    assert application.activity_type_id is not None
    assert application.contour_id is not None
    assert application.period_from is not None
    assert application.period_to is not None

    items = await repo.list_items(db, application.id)
    codes = {row.id: row.code for row in await admin_repo.list_livestock_types(db)}
    for item in items:
        if item.livestock_type_id not in codes:
            # Only reachable if a type was archived after the draft named it —
            # `_assert_references` refuses an unknown one at PATCH time.
            raise err("ERR-VAL-001", details={"reason": "unknown_livestock_type"})

    benefit_code: str | None = None
    if application.benefit_category_item_id is not None:
        benefit_item = await admin_repo.get_classifier_item(
            db, application.benefit_category_item_id
        )
        if benefit_item is None:
            raise err("ERR-VAL-001", details={"reason": "unknown_benefit_category"})
        benefit_code = benefit_item.code

    return CalculationIn(
        contour_id=application.contour_id,
        activity_type_id=application.activity_type_id,
        period_from=application.period_from,
        period_to=application.period_to,
        quantity=application.quantity,
        items=[
            LivestockItemIn(livestock_code=codes[item.livestock_type_id], count=item.head_count)
            for item in items
        ],
        benefit_code=benefit_code,
    )


def _skipped(check_types: list[str], details: dict[str, Any]) -> list[tuple[str, str, Any]]:
    return [(check_type, "skipped", details) for check_type in check_types]


async def _gis_results(db: AsyncSession, application: Application) -> list[tuple[str, str, Any]]:
    """The three topology checks, or `skipped` rows saying why they could not
    run.

    `gis.service.run_checks` — the actor-free, zone-free public-surface one —
    NOT `run_version_checks`, which is the HTTP path and applies the zone rule
    that would refuse an applicant outright.
    """
    if application.contour_id is None:
        return _skipped(list(GIS_CHECK_TYPES.values()), {"reason": "no_contour"})
    version = await gis_service.published_version(db, application.contour_id)
    if version is None:
        # A contour whose geometry is still a draft has nothing to check
        # against. Task 5 refuses a submission on it; here it is reported.
        return _skipped(list(GIS_CHECK_TYPES.values()), {"reason": "no_published_version"})
    return [
        (GIS_CHECK_TYPES[result["check"]], result["result"], result["details"])
        for result in await gis_service.run_checks(db, version.id)
        if result["check"] in GIS_CHECK_TYPES
    ]


async def _norm_results(
    db: AsyncSession, application: Application, norm_results: list[Any] | None
) -> list[tuple[str, str, Any]]:
    """The six admissibility checks, or `skipped` rows naming the fields that
    are still empty.

    `norm_results` is `norms.service.preview(...)["checks"]` when the caller has
    ALREADY run the pricing — the same list, computed off the same
    request/snapshot pair, except that its `limit` check carries the REAL
    comparison instead of `skipped`. Passing it is not an optimisation: with
    `used_sb=None`, `norms.checks._limit_check` reports `skipped` on purpose
    (its reviewer-facing path, "checks without the money"), so a `run_all` that
    always called `norms.service.run_checks` could never record that a herd is
    over the limit — and `first_blocking_error` could never refuse one.

    Left `None` — a caller with no price to hand — the unpriced
    `norms.service.run_checks` runs instead, and `norm_limit` is honestly
    recorded as `skipped`.

    **Since ruling #176 that last sentence holds for GRAZING ONLY.** The limit
    check is no longer grazing-only: every other activity compares
    `request.quantity`, which needs no pricing step, so the unpriced path now
    returns a real capacity — or exclusivity — verdict for them rather than
    `skipped`. That catches more than before and never fewer, so nothing
    downstream loosens; only this paragraph's old promise of a uniform
    `skipped` is gone.
    """
    missing = await missing_for_pricing(db, application)
    if missing:
        return _skipped(
            list(NORM_CHECK_TYPES.values()), {"reason": "incomplete", "missing": missing}
        )
    results = norm_results
    if results is None:
        results = await norms_service.run_checks(
            db, payload=await calculation_payload(db, application)
        )
    return [
        (NORM_CHECK_TYPES[result["check"]], result["result"], result["details"])
        for result in results
        if result["check"] in NORM_CHECK_TYPES
    ]


async def run_all(
    db: AsyncSession, application: Application, *, norm_results: list[Any] | None = None
) -> list[ApplicationCheck]:
    """Every check this application needs, recorded and returned.

    The rows are written WHATEVER they say (ruling 12) — a failing check is
    evidence, and evidence is kept. What a blocking failure then means is the
    caller's decision and the only difference between the two callers:
    `service.precheck` returns them as data, task 5's `submit` turns the first
    one into the HTTP error `first_blocking_error` maps it to.

    Does NOT audit: a check run is part of a larger action (`application.
    precheck`, `application.submit`) and audits under that action's own name,
    once, at the service entry point — never a second entry per row here.

    **`norm_results` is `norms.service.preview(...)["checks"]`, and a caller
    that prices the application must pass it** — the plan's signature for this
    function had no such parameter, and the parameter is why the pre-check's
    `checks` and its `calculation` can never describe two different requests.
    Two independent computations would also silently weaken the vocabulary:
    `norms.service.run_checks` resolves `used_sb=None` by design, so its `limit`
    is always `skipped`, and an over-limit herd would be RECORDED as unchecked
    and `first_blocking_error` would let it through. **Task 5's `submit` must
    therefore run `norms.service.preview` BEFORE `run_all` and hand its checks
    in**, not after it as the step order in that task's brief suggests —
    otherwise its own "a blocking check refuses here" test is refused by
    `save_calculation` two steps later, under a code the recorded evidence does
    not support.
    """
    collected = await _gis_results(db, application)
    collected.extend(await _norm_results(db, application, norm_results))
    rows = [
        ApplicationCheck(
            application_id=application.id,
            check_type=check_type,
            result=result,
            details=_jsonable(details),
            source=SOURCE_AUTO,
            # `created_by` is NOT NULL (migration 0025): an auto check has no
            # reviewer of its own, so it is attributed to the application's
            # owner — the same actor `precheck`/`submit` already audit under
            # (module boundary: this file never imports `service.py`, so it
            # reads the id off the `Application` row it was already handed,
            # never the caller's `actor`).
            created_by=application.submitted_by_user_id,
        )
        for check_type, result, details in collected
    ]
    await repo.add_checks(db, rows)
    return rows


def first_blocking_error(results: list[ApplicationCheck]) -> DomainError | None:
    """The first BLOCKING failure, in the order `run_all` produced them, mapped
    onto its `ERR-` code — or `None` when nothing refuses.

    **Used by the submission, never by the pre-check** (design/03): an applicant
    has to be able to SEE that the herd is 40 head over the limit, not merely be
    refused.

    Shaped exactly like `norms.checks.first_blocking_error`, including the part
    that matters most: `details` carries the WHOLE check list, not just the one
    that blocked, so a caller never has to re-run anything to learn why. The
    rows' own `details` were coerced JSON-safe on the way in (`_jsonable`),
    which is what makes them renderable here — `DomainError`'s response goes
    through Starlette's stock `json.dumps` with no encoder of its own (lesson).
    """
    for row in results:
        if row.check_type in BLOCKING and row.result == "fail":
            return err(
                _ERROR_BY_CHECK_TYPE[row.check_type],
                details={
                    "checks": [
                        {
                            "check_type": other.check_type,
                            "result": other.result,
                            "details": other.details,
                        }
                        for other in results
                    ]
                },
            )
    return None
