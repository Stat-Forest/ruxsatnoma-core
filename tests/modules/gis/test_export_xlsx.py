"""Stage 13: the contours/imports exports are the lists on paper — same
filters, same zone scoping, readable cells, the id last, NO geometry."""

import pytest
from httpx import AsyncClient
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import MediaFile
from app.modules.admin.models import Organization
from app.modules.gis.models import Contour, GisLayer
from tests.conftest import assert_export_cut, export_cap, xlsx_rows
from tests.modules.gis.conftest import make_contour, make_import, make_version, random_box_wkt

pytestmark = pytest.mark.asyncio

CONTOURS = "/api/v1/gis/contours/export.xlsx"
IMPORTS = "/api/v1/gis/imports/export.xlsx"


async def _published_contour(
    db: AsyncSession,
    layer: GisLayer,
    org: Organization,
    approval_doc: MediaFile,
    **contour_over,
) -> Contour:
    """A fresh contour with one published version — the plain shape most of
    these tests need, built from the module's own `make_contour`/
    `make_version` helpers rather than the package's `published_contour`
    fixture (which hands back the `ContourVersion`, not the `Contour` a
    `.number` assertion needs)."""
    contour = await make_contour(db, layer, org, **contour_over)
    await make_version(
        db,
        contour.id,
        random_box_wkt(),
        status="published",
        approval_doc_id=approval_doc.id,
        published_at=func.now(),
    )
    await db.flush()
    return contour


# --- contours --------------------------------------------------------------


async def test_the_contours_export_mirrors_the_list(
    applicant_client: AsyncClient,
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
) -> None:
    """The same rows as the list under the same `organization_id`, the
    list's `bbox` filter (a malformed one refused like the list's), labels
    rather than codes with the number first, and the cap."""
    contour = await _published_contour(db, contours_layer, leshoz, approval_doc)
    await _published_contour(db, contours_layer, leshoz, approval_doc)
    org = {"organization_id": str(leshoz.id)}

    listed = await applicant_client.get(f"/api/v1/gis/contours?organization_id={leshoz.id}")
    assert listed.status_code == 200, listed.text
    listed_ids = {row["id"] for row in listed.json()["items"]}
    assert listed_ids

    resp = await applicant_client.get(CONTOURS, params={**org, "lang": "ru"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.headers["x-export-truncated"] == "false"
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Номер" and headers[-1] == "ID"
    assert {str(row[-1]) for row in rows} == listed_ids

    _, rows = xlsx_rows(
        (await applicant_client.get(CONTOURS, params={**org, "lang": "uz_latn"})).content
    )
    by_number = {row[0]: row for row in rows}
    assert contour.number in by_number  # the human number first
    row = by_number[contour.number]
    assert row[3] == "Kontur"  # the kind label, not "contour"
    assert row[8] == "Yoʻq"  # over_allocated label, not a bare Python bool

    inside = await applicant_client.get(CONTOURS, params={**org, "bbox": "0,0,40,30"})
    outside = await applicant_client.get(CONTOURS, params={**org, "bbox": "60.0,41.4,60.1,41.5"})
    assert inside.status_code == 200, inside.text
    assert outside.status_code == 200, outside.text
    assert len(xlsx_rows(inside.content)[1]) >= 1
    assert xlsx_rows(outside.content)[1] == []
    assert (await applicant_client.get(CONTOURS, params={"bbox": "nonsense"})).status_code == 422

    with export_cap(1):
        resp = await applicant_client.get(CONTOURS, params=org)
        assert resp.headers["x-export-total"] == "2"
        assert_export_cut(resp, cap=1)


async def test_contours_export_is_empty_for_a_caller_with_no_matching_zone(
    region_scoped_client: AsyncClient,
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
) -> None:
    """`region_scoped_client` is zoned to a region `leshoz` does not belong
    to (`leshoz` sets no region at all) — the same empty-not-403 shape
    `test_the_list_is_filtered_for_a_region_scoped_actor` proves for the
    list."""
    await _published_contour(db, contours_layer, leshoz, approval_doc)

    resp = await region_scoped_client.get(CONTOURS)
    assert resp.status_code == 200, resp.text
    assert xlsx_rows(resp.content)[1] == []


# --- imports -----------------------------------------------------------------


async def test_the_imports_export_mirrors_the_list(
    gis_client: AsyncClient,
    rahbar_client: AsyncClient,
    pending_import,
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    gis_user,
) -> None:
    """The list's whole set and total, its `status` filter, labels and the
    filename (never the file id) in the cells, and the cap."""
    review_batch = await make_import(
        db, layer=contours_layer, org=leshoz, started_by=gis_user, data=b"{}"
    )
    review_batch.status = "review"
    await db.flush()
    await db.commit()

    # The test database is persistent, so earlier runs' batches are here too:
    # read the list at the server's page ceiling and compare against the
    # export's whole set — the page is a subset of the file, and both count
    # the same total.
    listed = await gis_client.get("/api/v1/gis/imports", params={"page_size": 100})
    assert listed.status_code == 200, listed.text
    listed_ids = {row["id"] for row in listed.json()["items"]}
    assert str(pending_import.id) in listed_ids

    resp = await gis_client.get(IMPORTS, params={"lang": "ru"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.headers["x-export-truncated"] == "false"
    assert resp.headers["x-export-total"] == str(listed.json()["total"])
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Создан" and headers[-1] == "ID"
    exported_ids = {str(row[-1]) for row in rows}
    assert listed_ids <= exported_ids
    assert len(exported_ids) == listed.json()["total"]

    _, rows = xlsx_rows((await gis_client.get(IMPORTS, params={"lang": "uz_latn"})).content)
    row = next(r for r in rows if str(r[-1]) == str(pending_import.id))
    assert row[1] == "Navbatda"  # the status label, not "pending"
    assert row[5] == "import.geojson"  # the filename, not a media_files uuid

    resp = await rahbar_client.get(IMPORTS, params={"status": "review"})
    assert resp.status_code == 200, resp.text
    assert str(review_batch.id) in {str(row[-1]) for row in xlsx_rows(resp.content)[1]}

    with export_cap(1):  # `pending_import` and `review_batch` at least, one fits
        resp = await gis_client.get(IMPORTS)
        assert int(resp.headers["x-export-total"]) >= 2
        assert_export_cut(resp, cap=1)


async def test_imports_export_is_empty_for_a_caller_zoned_elsewhere(
    org_scoped_rahbar_client: AsyncClient, pending_import
) -> None:
    """`pending_import` sits under `leshoz`; `org_scoped_rahbar_client` is
    zoned to `other_leshoz` — the same shape `test_the_import_list_is_zone_scoped`
    proves for the JSON list."""
    resp = await org_scoped_rahbar_client.get(IMPORTS)
    assert resp.status_code == 200, resp.text
    assert str(pending_import.id) not in {str(row[-1]) for row in xlsx_rows(resp.content)[1]}


async def test_imports_export_refuses_a_caller_with_neither_manage_nor_approve(
    applicant_client: AsyncClient,
) -> None:
    resp = await applicant_client.get(IMPORTS)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"
