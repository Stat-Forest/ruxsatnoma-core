"""Ruling 3: one basis document, one review, one publication for the whole batch —
because the Agency delivers whole leshozes and nobody issues a decree per contour."""

from sqlalchemy import select

from app.modules.gis.models import ContourVersion


async def test_a_batch_is_reviewed_approved_and_published_in_three_calls(
    gis_client, rahbar_client, processed_import
):
    iid = processed_import.id
    assert (await gis_client.post(f"/api/v1/gis/imports/{iid}/submit-review")).status_code == 200
    assert (await rahbar_client.post(f"/api/v1/gis/imports/{iid}/approve")).status_code == 200
    published = await rahbar_client.post(f"/api/v1/gis/imports/{iid}/publish")
    assert published.status_code == 200
    assert published.json() == {"published": 2, "blocked": []}


async def test_every_published_version_carries_the_batch_document(
    rahbar_client, db, approved_import
):
    await rahbar_client.post(f"/api/v1/gis/imports/{approved_import.id}/publish")
    rows = await db.execute(
        select(ContourVersion.approval_doc_id, ContourVersion.status).where(
            ContourVersion.import_id == approved_import.id
        )
    )
    for doc_id, status in rows.all():
        assert status == "published"
        assert doc_id == approved_import.approval_doc_id


async def test_one_overlapping_feature_does_not_block_the_other_150(
    rahbar_client, db, approved_import_with_one_overlap
):
    result = await rahbar_client.post(
        f"/api/v1/gis/imports/{approved_import_with_one_overlap.id}/publish",
    )
    body = result.json()
    assert body["published"] == 1
    assert len(body["blocked"]) == 1
    assert body["blocked"][0]["checks"][-1]["check"] == "overlap"
    await db.refresh(approved_import_with_one_overlap)
    assert (
        approved_import_with_one_overlap.status == "approved"
    )  # not done — something is left to fix


async def test_publishing_a_batch_requires_the_approve_permission(gis_client, approved_import):
    resp = await gis_client.post(f"/api/v1/gis/imports/{approved_import.id}/publish")
    assert resp.status_code == 403
