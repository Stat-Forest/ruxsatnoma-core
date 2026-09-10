"""The pre-check: it REPORTS, it never refuses (plan 03.9a task 4).

`POST /applications/{id}/precheck` is the read-only twin of task 5's
submission — both run `checks.run_all`, and the whole difference between them
is what a BLOCKING result means: data here, an HTTP error there (design/03,
and 3.7's own `calc_router` docstring). A broken INPUT is still an HTTP error
on both paths.
"""

import uuid
from decimal import Decimal

from sqlalchemy import func, select

from app.modules.norms.models import Calculation


async def test_an_over_limit_herd_is_reported_not_refused(
    applicant_client, filing_ready_for_submission, published_grazing_norm, sheep_type_id
) -> None:
    """design/03: 'ERR-NORM-001..003 inside `checks`, not as an HTTP error'.
    The applicant must see the remaining limit, not just a 422."""
    over_limit = {
        **filing_ready_for_submission,
        "items": [{"livestock_type_id": str(sheep_type_id), "head_count": 100_000}],
    }
    result = await applicant_client.post("/api/v1/applications/precheck", json=over_limit)
    assert result.status_code == 200, result.text
    limit = next(c for c in result.json()["checks"] if c["check_type"] == "norm_limit")
    assert limit["result"] == "fail"
    # Renamed by ruling #176: the check is no longer grazing-only, so the
    # capacity and what is left of it are stated in the activity's own unit.
    assert "capacity" in limit["details"]
    assert "remaining" in limit["details"]


async def test_a_precheck_on_a_returned_application_writes_a_new_row_every_time(
    db, applicant_client, submitted_application
) -> None:
    """Ruling 12: a repeat check on a PERSISTED application is a NEW row. The
    reviewer has to be able to see that the contour passed at submission even
    if it would fail today. Since stage 12 the per-id route serves RETURNED
    (plan 12, R5); the filing itself recorded the first set."""
    from tests.modules.applications.test_application_api import _returned

    await _returned(db, submitted_application)
    app_id = submitted_application
    await applicant_client.post(f"/api/v1/applications/{app_id}/precheck")
    await applicant_client.post(f"/api/v1/applications/{app_id}/precheck")
    card = (await applicant_client.get(f"/api/v1/applications/{app_id}")).json()
    overlap = [c for c in card["checks"] if c["check_type"] == "gis_overlap"]
    assert len(overlap) == 3, "the filing's own row, then one per pre-check"


async def test_a_precheck_never_creates_an_application_or_stores_a_calculation(
    db, applicant_client, filing_ready_for_submission
) -> None:
    """Ruling 8: exactly one calculation is written, at the filing. A
    speculative row before it would be the one 3.10 bills from — and since
    stage 12 there is no row at all to bind one to (plan 12, R3).

    **The zero below is checked against a control, because on its own it used
    to be vacuous** (stage 3.7, finding I4): filing the very same body
    afterwards and seeing the count go to exactly ONE is what proves the zero
    was the pre-check's restraint and not the query's blindness.
    """
    from app.modules.applications.models import Application
    from tests.modules.applications.test_submit import _submit

    async def count(model) -> int:
        return await db.scalar(select(func.count()).select_from(model))

    applications_before = await count(Application)
    calculations_before = await count(Calculation)
    result = await applicant_client.post(
        "/api/v1/applications/precheck", json=filing_ready_for_submission
    )
    assert result.status_code == 200, result.text
    assert result.json()["calculation"]["amount"] is not None
    assert await count(Application) == applications_before
    assert await count(Calculation) == calculations_before

    submitted = await _submit(applicant_client, filing_ready_for_submission)
    assert submitted.status_code == 201, submitted.text
    assert await count(Calculation) == calculations_before + 1, (
        "the query finds a bound calculation when there is one — so the zero above "
        "is the pre-check writing nothing, not the read being unable to see it"
    )


async def test_a_half_empty_filing_is_answered_with_skipped_rows_not_an_error(
    applicant_client, applicant, published_contour
) -> None:
    """The wizard pre-checks whatever it holds, so the route is reached on an
    incomplete filing. `skipped` with a named reason is the established idiom
    of both check modules; a 500 or an empty list is not."""
    result = await applicant_client.post(
        "/api/v1/applications/precheck", json={"on_behalf": "self"}
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["calculation"] is None
    by_type = {c["check_type"]: c for c in body["checks"]}
    assert by_type["gis_within_fund"]["result"] == "skipped"
    assert by_type["gis_within_fund"]["details"]["reason"] == "no_contour"
    assert by_type["norm_available"]["result"] == "skipped"
    assert "contour_id" in by_type["norm_available"]["details"]["missing"]

    result = await applicant_client.post(
        "/api/v1/applications/precheck",
        json={"on_behalf": "self", "contour_id": str(published_contour.id)},
    )
    assert result.status_code == 200, result.text
    by_type = {c["check_type"]: c for c in result.json()["checks"]}
    assert by_type["gis_within_fund"]["result"] in {"pass", "fail", "warning", "skipped"}
    assert by_type["norm_available"]["result"] == "skipped"
    assert "activity_type_id" in by_type["norm_available"]["details"]["missing"]


async def test_a_stranger_cannot_precheck_my_application(
    other_applicant_client, submitted_application
) -> None:
    """Ownership leaks are 404, never 403 — the per-id pre-check would
    otherwise confirm that this id is an application and price somebody
    else's plot."""
    refused = await other_applicant_client.post(
        f"/api/v1/applications/{submitted_application}/precheck"
    )
    assert refused.status_code == 404


async def test_the_check_vocabulary_is_ruling_21s_and_drops_gis_restrictions(
    applicant_client, filing_ready_for_submission, published_grazing_norm
) -> None:
    """Ruling 21: nine check types, and `gis`'s own `restrictions` result is
    NOT among them — `norm_restrictions` is the period-aware equivalent that
    supersedes it. `vet`/`cadastre` are 3.9b's."""
    result = await applicant_client.post(
        "/api/v1/applications/precheck", json=filing_ready_for_submission
    )
    assert result.status_code == 200, result.text
    written = [c["check_type"] for c in result.json()["checks"]]

    assert written == [
        "gis_validity",
        "gis_within_fund",
        "gis_overlap",
        "norm_available",
        "norm_season",
        "norm_rotation",
        "norm_fire_ban",
        "norm_restrictions",
        "norm_limit",
    ]


async def test_first_blocking_error_maps_each_blocking_type_and_ignores_a_warning() -> None:
    """`first_blocking_error` is the submission's half of ruling 21 (task 5
    calls it; the pre-check never does), so it is proven here beside the
    vocabulary it reads."""
    from app.modules.applications import checks
    from app.modules.applications.models import ApplicationCheck

    def row(check_type: str, result: str) -> ApplicationCheck:
        return ApplicationCheck(
            application_id=uuid.uuid4(), check_type=check_type, result=result, details={}
        )

    assert checks.first_blocking_error([row("gis_overlap", "pass")]) is None
    # A warning-only type never refuses, however it lands.
    assert checks.first_blocking_error([row("norm_restrictions", "fail")]) is None

    for check_type, code in (
        ("gis_validity", "ERR-GIS-001"),
        ("gis_within_fund", "ERR-GIS-002"),
        ("gis_overlap", "ERR-GIS-005"),
        ("norm_available", "ERR-NORM-001"),
        ("norm_limit", "ERR-NORM-002"),
        ("norm_season", "ERR-NORM-003"),
        ("norm_rotation", "ERR-NORM-003"),
        ("norm_fire_ban", "ERR-NORM-006"),
    ):
        error = checks.first_blocking_error([row("gis_validity", "pass"), row(check_type, "fail")])
        assert error is not None, check_type
        assert error.code == code

    # The whole list travels in `details`, JSON-safe, so a caller never has to
    # re-run anything to see why.
    error = checks.first_blocking_error([row("norm_limit", "fail"), row("gis_overlap", "pass")])
    assert error is not None and error.details is not None
    assert [c["check_type"] for c in error.details["checks"]] == ["norm_limit", "gis_overlap"]


async def test_a_real_overlap_is_reported_with_its_raw_decimal_and_uuid_details(
    db,
    applicant_client,
    applicant,
    overlapping_published_contour,
    grazing_activity_id,
    sheep_type_id,
    published_coef_sb,
) -> None:
    """The one path that carries UNCOERCED values: `gis.checks._intersections`
    puts a raw `uuid.UUID` (`feature_id`) and a raw `Decimal` (`area_m2`) into
    `details["items"]`, and nothing between there and the JSONB column would
    convert them — `checks._jsonable` is the whole defence, and without it this
    request is a 500 from inside the bind (lesson: nothing in this app
    configures a JSON encoder).

    It is also the GIS half of "reports rather than refuses": `gis_overlap` is a
    BLOCKING type, so a submission would be refused `ERR-GIS-005`, while the
    pre-check answers 200 and hands the applicant the overlapping contour.
    """
    filing = {
        "on_behalf": "self",
        "contour_id": str(overlapping_published_contour.id),
        "activity_type_id": str(grazing_activity_id),
        "period_from": "2027-05-01",
        "period_to": "2027-09-30",
        "items": [{"livestock_type_id": str(sheep_type_id), "head_count": 40}],
    }
    result = await applicant_client.post("/api/v1/applications/precheck", json=filing)
    assert result.status_code == 200, result.text

    overlap = next(c for c in result.json()["checks"] if c["check_type"] == "gis_overlap")
    assert overlap["result"] == "fail"
    item = overlap["details"]["items"][0]
    assert uuid.UUID(item["feature_id"])
    assert Decimal(item["area_m2"]) > 0

    # And it survives the round trip through JSONB onto the card.
    # The same coercion guards the audit entry the stateless pre-check writes.
    from app.modules.audit.models import AuditLog

    audited = (
        (
            await db.execute(
                select(AuditLog)
                .where(
                    AuditLog.action == "application.precheck",
                    AuditLog.object_type == "application_filing",
                )
                .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            )
        )
        .scalars()
        .first()
    )
    assert audited is not None
    assert any(c["check_type"] == "gis_overlap" for c in audited.new_value["checks"])


async def test_a_tariff_exempt_activity_still_has_to_declare_its_quantity(
    applicant_client, applicant, published_contour, science_activity_id
) -> None:
    """Minor 3 of the task-4 review, settled: `quantity` is required for EVERY
    non-grazing activity, `science` included, even though
    `norms.calculator` never reads it for a `tariff_exempt:` one and would
    happily bill zero without it.

    The amount is a requisite of the printed permit (`tz/13` 1-ilova), and task
    5's submission gate requires it — a pre-check that said "ready" here would
    be contradicted by `ERR-APP-001` at submission, which is the one thing this
    route exists to prevent.
    """
    filing = {
        "on_behalf": "self",
        "contour_id": str(published_contour.id),
        "activity_type_id": str(science_activity_id),
        "period_from": "2027-05-01",
        "period_to": "2027-09-30",
    }
    without = await applicant_client.post("/api/v1/applications/precheck", json=filing)
    assert without.status_code == 200, without.text
    assert without.json()["calculation"] is None
    norm = next(c for c in without.json()["checks"] if c["check_type"] == "norm_available")
    assert norm["result"] == "skipped"
    assert norm["details"]["missing"] == ["quantity"]

    with_quantity = await applicant_client.post(
        "/api/v1/applications/precheck", json={**filing, "quantity": "12.5"}
    )
    assert with_quantity.status_code == 200, with_quantity.text
    # Un-rated by law, so priced at zero — but priced, not refused and not null.
    assert Decimal(with_quantity.json()["calculation"]["amount"]) == 0


async def test_every_written_row_is_source_auto_and_the_run_is_audited_once(
    db, applicant_client, submitted_application
) -> None:
    """Two explicit requirements of the task with nothing else asserting them:
    3.9a writes `source='auto'` on every check row (`manual_fallback` and
    `external_api` are 3.9b's), and a pre-check writes `application_checks` rows
    so it is a state-changing action and audits like one — ONCE, under this
    module's own constant, never a row per check."""
    from sqlalchemy import select

    from app.modules.applications.service import APPLICATION_PRECHECK
    from app.modules.audit.models import AuditLog
    from tests.modules.applications.test_application_api import _returned

    await _returned(db, submitted_application)
    app_id = submitted_application
    result = await applicant_client.post(f"/api/v1/applications/{app_id}/precheck")
    assert result.status_code == 200, result.text
    # Stage 12: the pre-check answers the light `PrecheckCheckOut` shape, so
    # `source` is read off the STORED rows the card serves, which is where the
    # requirement lives anyway.
    card = await applicant_client.get(f"/api/v1/applications/{app_id}")
    assert card.status_code == 200, card.text
    assert len(card.json()["checks"]) == 2 * len(result.json()["checks"]), "the filing's + this"
    assert {c["source"] for c in card.json()["checks"]} == {"auto"}

    entries = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == APPLICATION_PRECHECK,
                    AuditLog.object_id == uuid.UUID(app_id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert entries[0].object_type == "application"
    assert len(entries[0].new_value["checks"]) == len(result.json()["checks"])


# --- Stage 12, B1: the pure half of `run_all`, and items from memory ---------


async def test_evaluate_computes_the_same_rows_run_all_records_but_writes_none(
    db, applicant_client, submitted_application
):
    """B1: `evaluate` is the pure half of `run_all` — identical (check_type,
    result) pairs, and not one `application_checks` row."""
    from app.modules.applications import checks, repo
    from app.modules.applications.models import ApplicationCheck

    application = await repo.get_application(db, uuid.UUID(submitted_application))
    assert application is not None
    count = (
        select(func.count())
        .select_from(ApplicationCheck)
        .where(ApplicationCheck.application_id == application.id)
    )
    before = await db.scalar(count)
    collected = await checks.evaluate(db, application)
    assert await db.scalar(count) == before, "evaluate must not write"
    recorded = await checks.run_all(db, application)
    assert [(c, r) for c, r, _ in collected] == [(row.check_type, row.result) for row in recorded]


async def test_missing_for_pricing_reads_items_from_memory_when_handed_them(
    db, applicant, published_contour, grazing_activity_id, sheep_type_id
):
    """B1: a transient grazing application with in-memory items is complete;
    the same object with `items=[]` is missing them — and nothing was read
    from `application_items`, because the row was never inserted."""
    from datetime import date

    from app.modules.applications import checks
    from app.modules.applications.models import Application, ApplicationItem

    application = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        status="SUBMITTED",
        channel="portal",
        kind="new",
        activity_type_id=grazing_activity_id,
        contour_id=published_contour.id,
        period_from=date(2027, 5, 1),
        period_to=date(2027, 9, 30),
    )
    herd = [ApplicationItem(livestock_type_id=sheep_type_id, head_count=40)]
    assert "items" not in await checks.missing_for_pricing(db, application, items=herd)
    assert "items" in await checks.missing_for_pricing(db, application, items=[])
    payload = await checks.calculation_payload(db, application, items=herd)
    assert payload.items[0].count == 40
    collected = await checks.evaluate(db, application, items=herd)
    assert {c for c, _, _ in collected} >= {"norm_limit", "gis_within_fund"}
