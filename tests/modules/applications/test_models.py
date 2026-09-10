from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import uuid7
from app.modules.applications.models import (
    APPLICATION_STATUSES,
    CHECK_RESULTS,
    CHECK_TYPES,
    Application,
    ApplicationCheck,
    ApplicationStatusHistory,
)


async def _app(
    db,
    applicant,
    contour_id,
    activity_id,
    *,
    status="SUBMITTED",
    frm=date(2027, 5, 1),
    to=date(2027, 9, 30),
) -> Application:
    row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        activity_type_id=activity_id,
        contour_id=contour_id,
        period_from=frm,
        period_to=to,
        status=status,
        channel="portal",
    )
    db.add(row)
    await db.flush()
    return row


async def test_two_active_applications_on_an_overlapping_period_are_refused(
    db, applicant, published_contour, grazing_activity_id
) -> None:
    """tz/05 invariant 1: one active application per
    (applicant + contour + activity + overlapping period). The database is the
    only detector (ruling 6) — a pre-SELECT would race."""
    await _app(db, applicant, published_contour.id, grazing_activity_id)
    with pytest.raises(IntegrityError, match="ex_applications_no_duplicate"):
        await _app(
            db,
            applicant,
            published_contour.id,
            grazing_activity_id,
            frm=date(2027, 9, 1),
            to=date(2027, 11, 30),
        )


async def test_two_drafts_on_the_same_contour_are_allowed(
    db, applicant, published_contour, grazing_activity_id
) -> None:
    """DRAFT is outside the constraint's WHERE clause (design/02): a duplicate
    is caught at submission, not while the applicant is still typing."""
    await _app(db, applicant, published_contour.id, grazing_activity_id, status="DRAFT")
    await _app(db, applicant, published_contour.id, grazing_activity_id, status="DRAFT")


async def test_status_check_rejects_a_bogus_value(db, applicant) -> None:
    """I2: `ck_applications_status_valid` is the constraint ruling 2 built a whole
    paragraph around, because the plan's own earlier draft enumerated thirteen
    values after promising fourteen. Contour/activity/period left null (ruling 7)
    isolates the status CHECK from the EXCLUDE constraint entirely."""
    row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
        status="BOGUS",
    )
    db.add(row)
    with pytest.raises(IntegrityError, match="ck_applications_status_valid"):
        await db.flush()


async def test_status_check_accepts_every_status(db, applicant) -> None:
    """The positive half of I2: every OTHER test in this file only ever writes
    SUBMITTED or DRAFT, so a CHECK one status short of ruling 2's fourteen would
    still pass all of them. Driven off APPLICATION_STATUSES itself, never a
    retyped list — the two cannot drift apart by construction."""
    for status in APPLICATION_STATUSES:
        row = Application(
            applicant_id=applicant.id,
            submitted_by_user_id=applicant.owner_user_id,
            on_behalf="self",
            channel="portal",
            status=status,
        )
        db.add(row)
        await db.flush()


async def test_benefit_verification_status_check_rejects_a_bogus_value(db, applicant) -> None:
    """Ruling #179's own CHECK, the same shape as `status_valid` above — a
    SEPARATE state machine from `status`, so this needs its own guard."""
    row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
        status="DRAFT",
        benefit_verification_status="BOGUS",
    )
    db.add(row)
    with pytest.raises(IntegrityError, match="ck_applications_benefit_verification_status_valid"):
        await db.flush()


async def test_benefit_verification_status_defaults_to_not_required(db, applicant) -> None:
    """Every application, benefit or none, starts `not_required` — only
    `applications.service.submit` (out of this track's file ownership) ever
    writes `pending`."""
    row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
        status="DRAFT",
    )
    db.add(row)
    await db.flush()
    assert row.benefit_verification_status == "not_required"


async def test_check_type_check_rejects_a_bogus_value(db, applicant) -> None:
    """I2: `application_checks` had no test at all before this. `gis_restrictions`
    is design/02's own value, dropped by ruling 21 in favour of `norm_restrictions`
    — using it as the negative case doubles as a regression guard against it
    quietly coming back."""
    app_row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
    )
    db.add(app_row)
    await db.flush()
    check = ApplicationCheck(
        application_id=app_row.id,
        check_type="gis_restrictions",
        result="pass",
        details={},
        created_by=app_row.submitted_by_user_id,
    )
    db.add(check)
    with pytest.raises(IntegrityError, match="ck_application_checks_check_type_valid"):
        await db.flush()


async def test_check_type_check_accepts_every_check_type(db, applicant) -> None:
    """The positive half, ruling 21's eleven values, tuple-driven — the same
    one-short failure mode as the status CHECK, on a table nothing else in this
    file constructs at all."""
    app_row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
    )
    db.add(app_row)
    await db.flush()
    for check_type in CHECK_TYPES:
        check = ApplicationCheck(
            application_id=app_row.id,
            check_type=check_type,
            result="pass",
            details={},
            created_by=app_row.submitted_by_user_id,
        )
        db.add(check)
        await db.flush()


async def test_result_check_rejects_a_bogus_value(db, applicant) -> None:
    """The negative half: without it the positive test below would still pass
    against a table carrying no `result` CHECK at all."""
    app_row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
    )
    db.add(app_row)
    await db.flush()
    check = ApplicationCheck(
        application_id=app_row.id,
        check_type="gis_validity",
        result="unknown",
        details={},
        created_by=app_row.submitted_by_user_id,
    )
    db.add(check)
    with pytest.raises(IntegrityError, match="ck_application_checks_result_valid"):
        await db.flush()


async def test_result_check_accepts_every_result(db, applicant) -> None:
    """Final review C1. `CHECK_RESULTS` was the one enum-ish tuple on this table
    with no test driving every value through the database — and it was the one
    tuple actually a value short: both `gis.checks` and `norms.checks` emit
    `skipped`, and `gis.checks._within_fund` emits it for EVERY contour while the
    Agency's `forest_fund` layer is empty. Tuple-driven for the same reason as the
    status and check_type tests above: the CHECK and the tuple cannot drift."""
    app_row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
    )
    db.add(app_row)
    await db.flush()
    for result in CHECK_RESULTS:
        check = ApplicationCheck(
            application_id=app_row.id,
            check_type="gis_validity",
            result=result,
            details={},
            created_by=app_row.submitted_by_user_id,
        )
        db.add(check)
        await db.flush()


def test_check_results_cover_everything_the_check_modules_emit() -> None:
    """The gap that actually let C1 through, and the reason this is a separate
    test from the two above: a tuple-driven DB test proves the CHECK matches the
    tuple, never that the tuple matches its PRODUCERS. `application_checks` rows
    are written straight from what `gis.checks.run_checks` and
    `norms.checks.run_checks` returned (ruling 12), so a fifth `result` literal
    appearing in either module must fail HERE — on the branch that adds it — not
    as an `IntegrityError` 500 on a live submission, against a migration that is
    by then permanent.

    Reads the two modules' source instead of calling them: covering every branch
    for real would need a published contour, a norm and a fixture per outcome,
    and the source is where a new literal is actually introduced. Two shapes are
    collected — a literal under a `"result"` key, and a literal assigned to a
    local named `result` (`norms.checks._limit_check`'s ternary) — which is every
    emitter in both modules today. A literal reaching `result` through a helper
    call would escape this scan; there is none, and adding one is the shape to
    watch for."""
    import ast
    import pathlib

    emitted: set[str] = set()

    def collect(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            emitted.add(node.value)
        elif isinstance(node, ast.IfExp):  # `"fail" if ... else "pass"`
            collect(node.body)
            collect(node.orelse)

    for module in ("app/modules/gis/checks.py", "app/modules/norms/checks.py"):
        tree = ast.parse(pathlib.Path(module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "result":
                        collect(value)
            elif isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == "result" for t in node.targets):
                    collect(node.value)

    assert emitted, "no `result` literals found — did the check modules move?"
    assert "skipped" in emitted, "C1's own regression guard: `skipped` IS emitted"
    assert emitted <= set(CHECK_RESULTS), sorted(emitted - set(CHECK_RESULTS))


async def test_status_history_cannot_be_updated(
    db, applicant, published_contour, grazing_activity_id
) -> None:
    """tz/05 invariant 6 + the append-only idiom of audit_log and calculations.

    `DBAPIError`, not `IntegrityError`: the trigger's plain `RAISE EXCEPTION` has
    no SQLSTATE in the integrity-constraint-violation class, so it surfaces as the
    broader `DBAPIError` — the same as `tests/modules/audit/test_audit_log.py` and
    `tests/modules/norms/test_models.py` assert for the identical trigger idiom."""
    from app.modules.applications.models import ApplicationStatusHistory

    app_row = await _app(db, applicant, published_contour.id, grazing_activity_id)
    row = ApplicationStatusHistory(
        application_id=app_row.id, from_status=None, to_status="DRAFT", changed_by=None
    )
    db.add(row)
    await db.flush()
    row.to_status = "APPROVED"
    with pytest.raises(DBAPIError, match="append-only"):
        await db.flush()


async def test_status_history_cannot_be_deleted(
    db, applicant, published_contour, grazing_activity_id
) -> None:
    """M2: UPDATE and DELETE share one `CREATE TRIGGER ... FOR EACH ROW` statement
    so they stand or fall together in principle, but only UPDATE had a test."""
    app_row = await _app(db, applicant, published_contour.id, grazing_activity_id)
    row = ApplicationStatusHistory(
        application_id=app_row.id, from_status=None, to_status="DRAFT", changed_by=None
    )
    db.add(row)
    await db.flush()
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            text("DELETE FROM application_status_history WHERE id = :id"), {"id": row.id}
        )


async def test_status_history_cannot_be_truncated(db) -> None:
    """M2: `application_status_history_no_truncate` is a SEPARATE `CREATE TRIGGER`
    statement from the UPDATE/DELETE one (`FOR EACH STATEMENT`, not `FOR EACH
    ROW`) — a typo in its clause or its table name would ship unnoticed."""
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(text("TRUNCATE application_status_history"))


async def test_status_history_id_can_be_supplied_explicitly(
    db, applicant, published_contour, grazing_activity_id
) -> None:
    """M3 / ruling 25: `submit` supplies `application_status_history.id` explicitly
    — it is the submission id the signature is bound to. Asserted nowhere until
    now; `id` keeps its `uuid7` default (models.py), which a caller-supplied value
    simply overrides, the same as any other Python-side `mapped_column(default=)`."""
    app_row = await _app(db, applicant, published_contour.id, grazing_activity_id)
    submission_id = uuid7()
    row = ApplicationStatusHistory(
        id=submission_id,
        application_id=app_row.id,
        from_status=None,
        to_status="SUBMITTED",
        changed_by=None,
    )
    db.add(row)
    await db.flush()
    assert row.id == submission_id


async def test_applications_permission_seeds(db) -> None:
    """ruling 16: create -> applicant, review -> executor_staff ('hodim' in the
    plan's prose), decide -> executor_head, view_any -> prosecutor, assign ->
    sys_admin. Wrong role codes in the migration insert zero rows silently
    (.claude/lessons.md) — this is the guard.

    `decide` was granted to executor_head AND leadership by 0015 (review round 1
    finding I3, which caught that ruling 16's prose named only `leadership` while
    tz/03's matrix gives «Т» to Раҳбар = `executor_head`). **Migration 0016 revoked
    leadership's half** once Oybek settled the question — decision #59, option а —
    so the expected set below is the post-0016 state, not 0015's. **Task 5's fix
    round 1 (controller ruling) added `conclude_gis -> gis_specialist`** via
    migration 0025 — the code `kind="gis"` conclusions are gated on
    (`app/modules/applications/permissions.py`; `gis`'s own registry had no
    fit). The two guards in `tests/test_permissions_registry.py` assert the
    same alignment across all three stages that grant an approval code.

    **`r.is_system` scopes this to the eleven `0003_auth` seeds**, the same
    filter `tests/test_permissions_registry.py` applies and for the same reason:
    this database is shared and persistent, and a role invented by a test —
    `tests/modules/applications/conftest.py`'s three `test_head_limit_*` roles
    carry a COPY of `executor_head`'s grants, deliberately — says nothing about
    what a migration seeded."""
    rows = await db.execute(
        text(
            "SELECT r.code, rp.permission_code FROM role_permissions rp"
            " JOIN roles r ON r.id = rp.role_id"
            " WHERE rp.permission_code LIKE 'applications.%' AND r.is_system"
        )
    )
    assert {(row[0], row[1]) for row in rows} == {
        ("applicant", "applications.create"),
        ("executor_staff", "applications.review"),
        ("executor_head", "applications.decide"),
        ("prosecutor", "applications.view_any"),
        ("sys_admin", "applications.assign"),
        ("gis_specialist", "applications.conclude_gis"),
    }


def test_the_schema_literals_match_the_tuples_the_checks_are_built_from() -> None:
    """The one guard against `schemas.ApplicationStatus` and its siblings
    drifting from the tuples `models.py` builds its CHECK constraints from
    (lesson: an enum-ish column has ONE source of truth). The members have to be
    written out — pyright rejects a starred variable inside `Literal` — so a
    value added on one side and forgotten on the other would be a 422 that
    should have been a 200, or an `IntegrityError` 500 that should have been a
    422. `permits/test_models.py` carries the identical guard. Task 5 (3.9b)
    adds `ConclusionKind`/`ConclusionRecommendation` beside the original four."""
    from typing import get_args

    from app.modules.applications.models import (
        APPLICATION_KINDS,
        BENEFIT_VERIFICATION_STATUSES,
        CHANNELS,
        CONCLUSION_KINDS,
        CONCLUSION_RECOMMENDATIONS,
        ON_BEHALF_VALUES,
    )
    from app.modules.applications.schemas import (
        ApplicationKind,
        ApplicationStatus,
        BenefitVerificationStatus,
        Channel,
        ConclusionKind,
        ConclusionRecommendation,
        OnBehalf,
    )

    assert set(get_args(ApplicationStatus)) == set(APPLICATION_STATUSES)
    assert set(get_args(OnBehalf)) == set(ON_BEHALF_VALUES)
    assert set(get_args(Channel)) == set(CHANNELS)
    assert set(get_args(ApplicationKind)) == set(APPLICATION_KINDS)
    assert set(get_args(ConclusionKind)) == set(CONCLUSION_KINDS)
    assert set(get_args(ConclusionRecommendation)) == set(CONCLUSION_RECOMMENDATIONS)
    assert set(get_args(BenefitVerificationStatus)) == set(BENEFIT_VERIFICATION_STATUSES)
