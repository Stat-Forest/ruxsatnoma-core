"""Stage 12, B3: the pre-check and the package over a filing that exists
nowhere but in the request body (plan 12, R2/R3)."""

import base64
import json
import uuid
from decimal import Decimal

from sqlalchemy import func, select

from app.modules.applications.models import Application, ApplicationCheck

API = "/api/v1"


def _grazing_filing(contour_id, activity_type_id, livestock_type_id, **overrides):
    body = {
        "contour_id": str(contour_id),
        "activity_type_id": str(activity_type_id),
        "period_from": "2027-05-01",
        "period_to": "2027-09-30",
        "items": [{"livestock_type_id": str(livestock_type_id), "head_count": 40}],
    }
    body.update(overrides)
    return body


async def test_a_complete_filing_is_prechecked_and_priced_and_nothing_is_written(
    db,
    applicant_client,
    published_contour,
    grazing_activity_id,
    sheep_type_id,
    published_coef_sb,
    published_grazing_norm,
):
    apps_before = await db.scalar(select(func.count()).select_from(Application))
    checks_before = await db.scalar(select(func.count()).select_from(ApplicationCheck))
    result = await applicant_client.post(
        "/api/v1/applications/precheck",
        json=_grazing_filing(published_contour.id, grazing_activity_id, sheep_type_id),
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["calculation"] is not None and body["calculation"]["amount"]
    # The price is explained before the citizen signs over it, not only after.
    (line,) = body["calculation"]["lines"]
    assert line["quantity"] == "40" and body["calculation"]["bhm"]
    assert Decimal(line["amount"]) == Decimal(body["calculation"]["amount"])
    assert {row["check_type"] for row in body["checks"]} >= {"norm_limit", "gis_within_fund"}
    assert all(set(row) == {"check_type", "result", "details"} for row in body["checks"])
    assert await db.scalar(select(func.count()).select_from(Application)) == apps_before
    assert await db.scalar(select(func.count()).select_from(ApplicationCheck)) == checks_before


async def test_an_incomplete_filing_names_the_missing_fields_as_skipped_rows(
    applicant_client, published_contour, grazing_activity_id, sheep_type_id
):
    body = _grazing_filing(published_contour.id, grazing_activity_id, sheep_type_id)
    del body["period_to"]
    result = await applicant_client.post("/api/v1/applications/precheck", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["calculation"] is None
    skipped = [row for row in result.json()["checks"] if row["result"] == "skipped"]
    assert any("period_to" in json.dumps(row["details"]) for row in skipped)


async def test_the_package_mints_an_id_and_names_it_in_the_bytes(
    applicant_client,
    published_contour,
    grazing_activity_id,
    sheep_type_id,
    published_coef_sb,
    published_grazing_norm,
):
    filing = _grazing_filing(published_contour.id, grazing_activity_id, sheep_type_id)
    result = await applicant_client.post("/api/v1/applications/package", json=filing)
    assert result.status_code == 200, result.text
    application_id = uuid.UUID(result.json()["application_id"])
    package = json.loads(base64.b64decode(result.json()["package"]))
    assert package["application_id"] == str(application_id)
    assert package["contour_version_id"] and package["amount"]
    again = await applicant_client.post("/api/v1/applications/package", json=filing)
    assert again.json()["application_id"] != str(application_id), "a fresh id every call"


async def test_an_incomplete_filing_cannot_be_packaged(
    applicant_client, published_contour, grazing_activity_id, sheep_type_id
):
    body = _grazing_filing(published_contour.id, grazing_activity_id, sheep_type_id)
    body["items"] = []
    result = await applicant_client.post("/api/v1/applications/package", json=body)
    assert result.status_code == 400, result.text
    assert "items" in result.json()["error"]["details"]["missing"]


async def test_a_document_that_is_not_the_callers_upload_is_refused_at_precheck(
    applicant_client,
    other_applicant_client,
    published_contour,
    grazing_activity_id,
    sheep_type_id,
    benefit_doc_type_item_id,
):
    from tests.modules.applications.test_submit import _upload

    foreign_file = await _upload(other_applicant_client)
    body = _grazing_filing(
        published_contour.id,
        grazing_activity_id,
        sheep_type_id,
        documents=[{"doc_type_item_id": str(benefit_doc_type_item_id), "file_id": foreign_file}],
    )
    result = await applicant_client.post("/api/v1/applications/precheck", json=body)
    assert result.status_code == 422, result.text
    assert result.json()["error"]["details"]["reason"] == "document_file_not_owned"


async def test_the_static_routes_are_not_shadowed_by_the_uuid_routes(applicant_client):
    """`/applications/precheck` must not be parsed as `/applications/{id}`.

    Since decision #226 removed `on_behalf` (the last REQUIRED field on
    `ApplicationFilingIn`), an empty body no longer 422s at the schema —
    `precheck` REPORTS an incomplete filing as data (`checks[]`), it does not
    refuse it. Reaching that 200 with a `checks` key is what proves the route
    resolved to the precheck handler rather than being misparsed."""
    result = await applicant_client.post("/api/v1/applications/precheck", json={})
    assert result.status_code == 200, result.text
    assert "checks" in result.json()


async def test_a_transient_deadwood_filing_is_told_its_blank_lines_too(
    applicant_client, published_contour, deadwood_activity_id
) -> None:
    """The stage-12 path builds an `Application` that has no row; the same
    `missing_for_pricing` must name the same fields (one definition, R6)."""
    result = await applicant_client.post(
        f"{API}/applications/precheck",
        json={
            "activity_type_id": str(deadwood_activity_id),
            "contour_id": str(published_contour.id),
            "period_from": "2027-05-01",
            "period_to": "2027-05-31",
            "quantity": "3",
        },
    )
    assert result.status_code == 200, result.text
    skipped = [row for row in result.json()["checks"] if row["result"] == "skipped"]
    missing = {name for row in skipped for name in row["details"].get("missing", [])}
    assert {"deadwood_product", "removal_deadline"} <= missing
