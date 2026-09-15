"""Stage 13: the norms/tariffs/rule-parameters exports are the lists on
paper — same filters, readable cells, the id last. All three registers are
open to any authenticated user (no zone of their own), so the "no scope"
shape here is simply: a different, unrelated authenticated caller sees the
SAME rows the export shows to anyone else."""

import uuid

import pytest
from httpx import AsyncClient

from app.modules.gis.models import Contour
from tests.conftest import assert_export_cut, export_cap, xlsx_rows

pytestmark = pytest.mark.asyncio

NORMS = "/api/v1/norms/export.xlsx"
TARIFFS = "/api/v1/tariffs/export.xlsx"
PARAMETERS = "/api/v1/rule-parameters/export.xlsx"


async def _draft_norm(
    client: AsyncClient, contour_id: uuid.UUID, activity_id: uuid.UUID, **over
) -> dict:
    payload = {
        "contour_id": str(contour_id),
        "activity_type_id": str(activity_id),
        "yield_c_per_ha": "12.0",
        "season": {"windows": [{"from": "04-01", "to": "10-31"}]},
        "rotation": {"rest_years": []},
        "effective_from": "2030-01-01",
    } | over
    created = await client.post("/api/v1/norms", json=payload)
    assert created.status_code == 201, created.text
    return created.json()


def _ids(content: bytes) -> set[str]:
    return {str(row[-1]) for row in xlsx_rows(content)[1]}


# --- norms ---------------------------------------------------------------


async def test_the_norms_export_mirrors_the_list(
    gis_specialist_client: AsyncClient,
    applicant_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """The same rows as the list under the same `contour_id`, the list's
    activity filter, labels rather than codes with the contour number first,
    the cap — and, with no zone of its own (`router.py`'s module docstring),
    the SAME rows for a caller with no special grant: the export must not
    narrow (or widen) that."""
    grazing = await _draft_norm(gis_specialist_client, published_contour.id, grazing_activity_id)
    await _draft_norm(gis_specialist_client, published_contour.id, haymaking_activity_id)
    contour = {"contour_id": str(published_contour.id)}

    listed = await gis_specialist_client.get(f"/api/v1/norms?contour_id={published_contour.id}")
    assert listed.status_code == 200, listed.text
    listed_ids = {row["id"] for row in listed.json()["items"]}
    assert listed_ids

    resp = await gis_specialist_client.get(NORMS, params={**contour, "lang": "ru"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.headers["x-export-truncated"] == "false"
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Контур" and headers[-1] == "ID"
    assert {str(row[-1]) for row in rows} == listed_ids

    resp = await gis_specialist_client.get(
        NORMS, params={**contour, "activity_type_id": str(grazing_activity_id), "lang": "uz_latn"}
    )
    assert resp.status_code == 200, resp.text
    _, rows = xlsx_rows(resp.content)
    assert {str(row[-1]) for row in rows} == {grazing["id"]}
    (row,) = rows
    assert row[0] == published_contour.number  # the contour number first
    assert row[2] == "Qoralama"  # the status label, not "draft"

    with export_cap(1):  # two norms on this contour, one fits
        resp = await gis_specialist_client.get(NORMS, params=contour)
        assert resp.headers["x-export-total"] == "2"
        assert_export_cut(resp, cap=1)

    listed = await applicant_client.get(f"/api/v1/norms?contour_id={published_contour.id}")
    assert listed.status_code == 200
    assert {row["id"] for row in listed.json()["items"]} == listed_ids
    resp = await applicant_client.get(NORMS, params=contour)
    assert resp.status_code == 200, resp.text
    assert _ids(resp.content) == listed_ids


# --- tariffs ---------------------------------------------------------------
# The seeded VMQ 278 rates (`test_tariffs_api.py`'s own fixtures) are already
# in force from 2015-09-30 — haymaking (1.5) and grazing's four livestock
# groups — so this test reads them rather than creating new ones. No zone of
# its own: a plain applicant sees the same in-force rates the list shows
# anyone, which is why the applicant IS the caller here.


async def test_the_tariffs_export_mirrors_the_list(applicant_client: AsyncClient) -> None:
    grazing = {"activity_code": "grazing", "on_date": "2026-08-30"}
    listed = await applicant_client.get("/api/v1/tariffs", params=grazing)
    assert listed.status_code == 200, listed.text
    listed_ids = {row["id"] for row in listed.json()["items"]}
    assert listed_ids

    resp = await applicant_client.get(TARIFFS, params={**grazing, "lang": "ru"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-export-truncated"] == "false"
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Вид деятельности" and headers[-1] == "ID"
    assert {str(row[-1]) for row in rows} == listed_ids

    _, rows = xlsx_rows(
        (await applicant_client.get(TARIFFS, params={**grazing, "lang": "uz_latn"})).content
    )
    by_group = {row[1]: row for row in rows}
    assert "Yirik chorva, katta" in by_group  # the large_adult label, not the code
    row = by_group["Yirik chorva, katta"]
    assert row[5] == "Eʼlon qilingan"  # the status label, not "published"
    assert row[3] == "bosh"  # the quantity_unit label, not "head"

    # A date before any rate took effect: empty on both the list and the
    # export (`test_a_tariff_for_a_date_before_it_took_effect_is_not_returned`'s
    # own scenario).
    resp = await applicant_client.get(
        TARIFFS, params={"activity_code": "haymaking", "on_date": "2015-01-01"}
    )
    assert resp.status_code == 200, resp.text
    assert xlsx_rows(resp.content)[1] == []

    with export_cap(1):  # grazing has four published groups
        resp = await applicant_client.get(TARIFFS, params=grazing)
        assert int(resp.headers["x-export-total"]) >= 2
        assert_export_cut(resp, cap=1)


# --- rule parameters ---------------------------------------------------------


async def _draft_parameter(client: AsyncClient, code: str, value: str = "0.9") -> dict:
    created = await client.post(
        "/api/v1/rule-parameters",
        json={
            "code": code,
            "value": value,
            "effective_from": "2030-01-01",
            "basis": "test",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


async def test_the_parameters_export_mirrors_the_list(
    tariffs_maker_client: AsyncClient, applicant_client: AsyncClient, unique_suffix: str
) -> None:
    code = f"test_param_{unique_suffix}"
    await _draft_parameter(tariffs_maker_client, code)

    listed = await tariffs_maker_client.get("/api/v1/rule-parameters", params={"code": code})
    assert listed.status_code == 200, listed.text
    listed_ids = {row["id"] for row in listed.json()["items"]}
    assert listed_ids

    resp = await tariffs_maker_client.get(PARAMETERS, params={"code": code, "lang": "ru"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-export-truncated"] == "false"
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Код" and headers[-1] == "ID"
    assert {str(row[-1]) for row in rows} == listed_ids

    _, rows = xlsx_rows(
        (
            await tariffs_maker_client.get(PARAMETERS, params={"code": code, "lang": "uz_latn"})
        ).content
    )
    (row,) = rows
    assert row[0] == code  # the code first
    assert row[3] == "Qoralama"  # the status label, not "draft"
    assert row[7] == "Test User"  # created_by resolved to a name (make_user's default), not a uuid

    resp = await tariffs_maker_client.get(PARAMETERS, params={"code": f"other_{unique_suffix}"})
    assert resp.status_code == 200, resp.text
    assert xlsx_rows(resp.content)[1] == []

    # No `code` filter: the shared, persistent test DB always carries more
    # than one parameter (the seeded `coef_sb:*`/`bhm`/... rows, plus the one
    # just created), so `total` is guaranteed > the cap of 1 here.
    with export_cap(1):
        resp = await tariffs_maker_client.get(PARAMETERS)
        assert int(resp.headers["x-export-total"]) > 1
        assert_export_cut(resp, cap=1)

    # No zone of its own: any authenticated caller sees the same rows.
    listed = await applicant_client.get("/api/v1/rule-parameters", params={"code": code})
    assert {row["id"] for row in listed.json()["items"]} == listed_ids
    resp = await applicant_client.get(PARAMETERS, params={"code": code})
    assert resp.status_code == 200, resp.text
    assert _ids(resp.content) == listed_ids
