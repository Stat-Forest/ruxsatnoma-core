"""Stage 13: the norms/tariffs/rule-parameters exports are the lists on
paper — same filters, readable cells, the id last. All three registers are
open to any authenticated user (no zone of their own), so the "no scope"
shape here is simply: a different, unrelated authenticated caller sees the
SAME rows the export shows to anyone else."""

import io
import uuid

import pytest
from httpx import AsyncClient
from openpyxl import load_workbook

from app.modules.gis.models import Contour

pytestmark = pytest.mark.asyncio


def _sheet(content: bytes):
    sheet = load_workbook(io.BytesIO(content)).active
    assert sheet is not None
    return sheet


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


# --- norms ---------------------------------------------------------------


async def test_norms_export_holds_exactly_the_rows_the_list_shows(
    gis_specialist_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
) -> None:
    await _draft_norm(gis_specialist_client, published_contour.id, grazing_activity_id)

    listed = await gis_specialist_client.get(f"/api/v1/norms?contour_id={published_contour.id}")
    assert listed.status_code == 200, listed.text
    listed_ids = {row["id"] for row in listed.json()["items"]}
    assert listed_ids

    resp = await gis_specialist_client.get(
        "/api/v1/norms/export.xlsx",
        params={"contour_id": str(published_contour.id), "lang": "ru"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.headers["x-export-truncated"] == "false"
    sheet = _sheet(resp.content)
    headers = [c.value for c in sheet[1]]
    assert headers[0] == "Контур" and headers[-1] == "ID"
    exported_ids = {str(row[-1]) for row in sheet.iter_rows(min_row=2, values_only=True)}
    assert exported_ids == listed_ids


async def test_norms_export_applies_the_same_filter_as_the_list(
    gis_specialist_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    haymaking_activity_id: uuid.UUID,
) -> None:
    await _draft_norm(gis_specialist_client, published_contour.id, grazing_activity_id)

    resp = await gis_specialist_client.get(
        "/api/v1/norms/export.xlsx",
        params={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(haymaking_activity_id),
        },
    )
    assert resp.status_code == 200, resp.text
    assert list(_sheet(resp.content).iter_rows(min_row=2, values_only=True)) == []


async def test_norms_export_renders_labels_not_codes(
    gis_specialist_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
) -> None:
    await _draft_norm(gis_specialist_client, published_contour.id, grazing_activity_id)

    resp = await gis_specialist_client.get(
        "/api/v1/norms/export.xlsx",
        params={"contour_id": str(published_contour.id), "lang": "uz_latn"},
    )
    row = next(_sheet(resp.content).iter_rows(min_row=2, values_only=True))
    assert row[0] == published_contour.number  # the contour number first
    assert row[2] == "Qoralama"  # the status label, not "draft"


async def test_norms_export_truncates_at_the_cap_and_says_so(
    gis_specialist_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    haymaking_activity_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _draft_norm(gis_specialist_client, published_contour.id, grazing_activity_id)
    await _draft_norm(gis_specialist_client, published_contour.id, haymaking_activity_id)

    from app.core import settings_store

    real_get_int = settings_store.get_int

    async def one(db_arg, key):
        if key == "register_export_max_rows":
            return 1
        return await real_get_int(db_arg, key)

    monkeypatch.setattr(settings_store, "get_int", one)
    resp = await gis_specialist_client.get(
        "/api/v1/norms/export.xlsx", params={"contour_id": str(published_contour.id)}
    )
    assert resp.status_code == 200, resp.text
    total = int(resp.headers["x-export-total"])
    assert total >= 2
    assert resp.headers["x-export-truncated"] == "true"
    assert resp.headers["x-export-rows"] == "1"
    assert len(list(_sheet(resp.content).iter_rows(min_row=2, values_only=True))) == 1


async def test_norms_export_is_visible_to_any_authenticated_caller(
    gis_specialist_client: AsyncClient,
    applicant_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
) -> None:
    """No zone of its own (`router.py`'s module docstring): a caller with no
    special grant sees the SAME rows the list shows it — the export must
    not narrow (or widen) that."""
    await _draft_norm(gis_specialist_client, published_contour.id, grazing_activity_id)

    listed = await applicant_client.get(f"/api/v1/norms?contour_id={published_contour.id}")
    assert listed.status_code == 200
    listed_ids = {row["id"] for row in listed.json()["items"]}
    assert listed_ids

    resp = await applicant_client.get(
        "/api/v1/norms/export.xlsx", params={"contour_id": str(published_contour.id)}
    )
    assert resp.status_code == 200, resp.text
    exported_ids = {
        str(row[-1]) for row in _sheet(resp.content).iter_rows(min_row=2, values_only=True)
    }
    assert exported_ids == listed_ids


async def test_norms_export_rejects_an_unknown_language(
    gis_specialist_client: AsyncClient,
) -> None:
    resp = await gis_specialist_client.get("/api/v1/norms/export.xlsx", params={"lang": "en"})
    assert resp.status_code == 422
