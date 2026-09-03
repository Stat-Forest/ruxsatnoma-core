"""The pre-check: it REPORTS, it never refuses (plan 03.9a task 4).

`POST /applications/{id}/precheck` is the read-only twin of task 5's
submission — both run `checks.run_all`, and the whole difference between them
is what a BLOCKING result means: data here, an HTTP error there (design/03,
and 3.7's own `calc_router` docstring). A broken INPUT is still an HTTP error
on both paths.
"""

import uuid

from sqlalchemy import func, select

from app.modules.norms.models import Calculation


async def test_an_over_limit_herd_is_reported_not_refused(
    applicant_client, draft_ready_for_submission, published_grazing_norm, sheep_type_id
) -> None:
    """design/03: 'ERR-NORM-001..003 inside `checks`, not as an HTTP error'.
    The applicant must see the remaining limit, not just a 422."""
    app_id = draft_ready_for_submission
    await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={"items": [{"livestock_type_id": str(sheep_type_id), "head_count": 100_000}]},
    )

    result = await applicant_client.post(f"/api/v1/applications/{app_id}/precheck")
    assert result.status_code == 200, result.text

    limit = next(c for c in result.json()["checks"] if c["check_type"] == "norm_limit")
    assert limit["result"] == "fail"
    assert "max_sb" in limit["details"]


async def test_a_precheck_writes_a_new_row_every_time(
    db, applicant_client, draft_ready_for_submission
) -> None:
    """Ruling 12: a repeat check is a NEW row. The reviewer has to be able to
    see that the contour passed at submission even if it would fail today."""
    app_id = draft_ready_for_submission
    await applicant_client.post(f"/api/v1/applications/{app_id}/precheck")
    await applicant_client.post(f"/api/v1/applications/{app_id}/precheck")

    card = (await applicant_client.get(f"/api/v1/applications/{app_id}")).json()
    overlap = [c for c in card["checks"] if c["check_type"] == "gis_overlap"]
    assert len(overlap) == 2


async def test_a_precheck_never_changes_the_status_or_stores_a_calculation(
    db, applicant_client, draft_ready_for_submission
) -> None:
    """Ruling 8: exactly one calculation is written, at submission. A
    speculative row before it would be the one 3.10 bills from."""
    app_id = draft_ready_for_submission
    result = await applicant_client.post(f"/api/v1/applications/{app_id}/precheck")
    assert result.json()["calculation"]["amount"] is not None

    card = (await applicant_client.get(f"/api/v1/applications/{app_id}")).json()
    assert card["status"] == "DRAFT"

    stored = await db.scalar(
        select(func.count()).select_from(Calculation).where(Calculation.application_id == app_id)
    )
    assert stored == 0


async def test_a_half_empty_draft_is_answered_with_skipped_rows_not_an_error(
    applicant_client, published_contour
) -> None:
    """A draft is autosaved field by field (ruling 7), so the pre-check is
    reached constantly on an incomplete one. `skipped` with a named reason is
    the established idiom of both check modules; a 500 or an empty list is
    not."""
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]

    result = await applicant_client.post(f"/api/v1/applications/{app_id}/precheck")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["calculation"] is None
    by_type = {c["check_type"]: c for c in body["checks"]}
    assert by_type["gis_within_fund"]["result"] == "skipped"
    assert by_type["gis_within_fund"]["details"]["reason"] == "no_contour"
    assert by_type["norm_available"]["result"] == "skipped"
    assert "contour_id" in by_type["norm_available"]["details"]["missing"]

    # The contour alone is still not enough for the norm half.
    await applicant_client.patch(
        f"/api/v1/applications/{app_id}", json={"contour_id": str(published_contour.id)}
    )
    result = await applicant_client.post(f"/api/v1/applications/{app_id}/precheck")
    assert result.status_code == 200, result.text
    by_type = {c["check_type"]: c for c in result.json()["checks"]}
    assert by_type["gis_within_fund"]["result"] in {"pass", "fail", "warning", "skipped"}
    assert by_type["norm_available"]["result"] == "skipped"
    assert "activity_type_id" in by_type["norm_available"]["details"]["missing"]


async def test_a_stranger_cannot_precheck_my_draft(
    applicant_client, other_applicant_client, draft_ready_for_submission
) -> None:
    """Ownership leaks are 404, never 403 — the pre-check would otherwise
    confirm that this id is an application and price somebody else's plot."""
    refused = await other_applicant_client.post(
        f"/api/v1/applications/{draft_ready_for_submission}/precheck"
    )
    assert refused.status_code == 404


async def test_the_check_vocabulary_is_ruling_21s_and_drops_gis_restrictions(
    applicant_client, draft_ready_for_submission, published_grazing_norm
) -> None:
    """Ruling 21: nine check types, and `gis`'s own `restrictions` result is
    NOT among them — `norm_restrictions` is the period-aware equivalent that
    supersedes it. `vet`/`cadastre` are 3.9b's."""
    result = await applicant_client.post(
        f"/api/v1/applications/{draft_ready_for_submission}/precheck"
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
