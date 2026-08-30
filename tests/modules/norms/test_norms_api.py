"""The norm read and edit surface: filtering, paging, and what a PATCH may
touch at each stage of the lifecycle. The lifecycle itself (draft -> review ->
approved -> published -> archived, who may do what, MaxSB) is
test_norm_lifecycle.py's own territory."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import MediaFile
from app.modules.gis.models import Contour

pytestmark = pytest.mark.asyncio


async def _draft(client: AsyncClient, contour_id: uuid.UUID, activity_id: uuid.UUID, **over):
    payload = {
        "contour_id": str(contour_id),
        "activity_type_id": str(activity_id),
        "yield_c_per_ha": "12.0",
        "season": {"windows": [{"from": "04-01", "to": "10-31"}]},
        "rotation": {"rest_years": []},
        "effective_from": "2030-01-01",
    } | over
    return await client.post("/api/v1/norms", json=payload)


async def test_the_list_filters_by_contour_and_pages(
    gis_specialist_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
) -> None:
    for effective_from in ("2030-01-01", "2031-01-01", "2032-01-01"):
        created = await _draft(
            gis_specialist_client,
            published_contour.id,
            grazing_activity_id,
            effective_from=effective_from,
        )
        assert created.status_code == 201, created.text

    listed = await gis_specialist_client.get(
        f"/api/v1/norms?contour_id={published_contour.id}&limit=2"
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


async def test_the_list_filters_by_activity_type_and_status(
    gis_specialist_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    haymaking_activity_id: uuid.UUID,
) -> None:
    await _draft(gis_specialist_client, published_contour.id, grazing_activity_id)
    hay = await _draft(gis_specialist_client, published_contour.id, haymaking_activity_id)
    assert hay.status_code == 201, hay.text

    listed = await gis_specialist_client.get(
        f"/api/v1/norms?contour_id={published_contour.id}"
        f"&activity_type_id={haymaking_activity_id}&status=draft"
    )
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["activity_type_id"] == str(haymaking_activity_id)
    assert items[0]["status"] == "draft"


async def test_patch_is_allowed_in_draft_and_review_but_refused_once_approved(
    gis_specialist_client: AsyncClient,
    leadership_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    survey_doc,
    approval_doc,
) -> None:
    created = await _draft(
        gis_specialist_client,
        published_contour.id,
        grazing_activity_id,
        geobotanic_doc_id=str(survey_doc.id),
    )
    assert created.status_code == 201, created.text
    norm_id = created.json()["id"]

    patched_draft = await gis_specialist_client.patch(
        f"/api/v1/norms/{norm_id}", json={"yield_c_per_ha": "15.0"}
    )
    assert patched_draft.status_code == 200, patched_draft.text
    assert patched_draft.json()["yield_c_per_ha"] == "15.0000"
    assert patched_draft.json()["status"] == "draft"

    review = await gis_specialist_client.post(f"/api/v1/norms/{norm_id}/submit-review")
    assert review.status_code == 200, review.text

    patched_review = await gis_specialist_client.patch(
        f"/api/v1/norms/{norm_id}", json={"yield_c_per_ha": "16.0"}
    )
    assert patched_review.status_code == 200, patched_review.text
    assert patched_review.json()["yield_c_per_ha"] == "16.0000"

    approved = await leadership_client.post(
        f"/api/v1/norms/{norm_id}/approve", json={"approval_doc_id": str(approval_doc.id)}
    )
    assert approved.status_code == 200, approved.text

    blocked = await gis_specialist_client.patch(
        f"/api/v1/norms/{norm_id}", json={"yield_c_per_ha": "17.0"}
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["details"]["reason"] == "not_draft"


async def test_getting_an_unknown_id_is_a_404(gis_specialist_client: AsyncClient) -> None:
    response = await gis_specialist_client.get(f"/api/v1/norms/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR-SYS-003"


async def test_archiving_a_published_norm_closes_its_effective_to(
    gis_specialist_client: AsyncClient,
    leadership_client: AsyncClient,
    central_admin_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    survey_doc,
    approval_doc,
) -> None:
    created = await _draft(
        gis_specialist_client,
        published_contour.id,
        grazing_activity_id,
        geobotanic_doc_id=str(survey_doc.id),
    )
    norm_id = created.json()["id"]
    assert created.json()["effective_to"] is None
    await gis_specialist_client.post(f"/api/v1/norms/{norm_id}/submit-review")
    await leadership_client.post(
        f"/api/v1/norms/{norm_id}/approve", json={"approval_doc_id": str(approval_doc.id)}
    )
    published = await central_admin_client.post(f"/api/v1/norms/{norm_id}/publish")
    assert published.status_code == 200, published.text

    archived = await leadership_client.post(f"/api/v1/norms/{norm_id}/archive")
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"
    assert archived.json()["effective_to"] is not None


async def test_the_create_response_shows_the_stored_precision_not_the_caller_s(
    gis_specialist_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
) -> None:
    """Same fixed-scale-NUMERIC lesson as the tariffs/parameters API:
    `yield_c_per_ha` is `numeric(10,4)`, so posting `"7.5"` must come back as
    `"7.5000"` — the value every other read of this row will show — not echo
    the caller's own unpadded string."""
    response = await _draft(
        gis_specialist_client,
        published_contour.id,
        grazing_activity_id,
        yield_c_per_ha="7.5",
    )
    assert response.status_code == 201, response.text
    assert response.json()["yield_c_per_ha"] == "7.5000"


async def test_reading_a_norm_needs_no_special_permission(
    applicant_client: AsyncClient, published_contour: Contour
) -> None:
    """`GET /norms` is how a front-end explains a limit; any authenticated
    user may read it. Writing is what the permission gates."""
    listed = await applicant_client.get(f"/api/v1/norms?contour_id={published_contour.id}")
    assert listed.status_code == 200


async def test_creating_a_norm_with_an_unknown_activity_type_is_refused(
    gis_specialist_client: AsyncClient, published_contour: Contour
) -> None:
    """A garbage `activity_type_id` must not reach `flush()` and surface as
    an uncaught `IntegrityError` -> `ERR-SYS-001`/500 (fix round 1)."""
    response = await _draft(gis_specialist_client, published_contour.id, uuid.uuid4())
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["reason"] == "unknown_activity_type"


async def test_creating_a_norm_with_an_archived_geobotanic_doc_is_refused(
    db: AsyncSession,
    gis_specialist_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
) -> None:
    """`_assert_doc_active` — already used by `approve_norm` for
    `approval_doc_id` — is reused here for `geobotanic_doc_id` at create time
    (fix round 1): an existence check, not a validity check (lesson), but an
    archived row must still be refused."""
    archived = MediaFile(
        storage_key=f"t/{uuid.uuid4().hex}",
        filename="old-survey.pdf",
        content_type="application/pdf",
        size_bytes=100,
        sha256="2" * 64,
        status="archived",
    )
    db.add(archived)
    await db.flush()

    response = await _draft(
        gis_specialist_client,
        published_contour.id,
        grazing_activity_id,
        geobotanic_doc_id=str(archived.id),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["reason"] == "geobotanic_doc_required"


async def test_creating_a_norm_with_effective_to_before_effective_from_is_refused(
    gis_specialist_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
) -> None:
    """Caught here, not by the `period_valid` DB CHECK (fix round 1) — an
    `IntegrityError` has no handler in `main.py` and would surface as a 500."""
    response = await _draft(
        gis_specialist_client,
        published_contour.id,
        grazing_activity_id,
        effective_from="2030-06-01",
        effective_to="2030-01-01",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["reason"] == "effective_to_before_from"
