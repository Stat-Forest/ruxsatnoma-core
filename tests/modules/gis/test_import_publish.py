"""Ruling 3: one basis document, one review, one publication for the whole batch —
because the Agency delivers whole leshozes and nobody issues a decree per contour."""

import uuid

from sqlalchemy import delete, select

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
    # Row-level check, not just the counters (review finding 3): the reported
    # blocked version must itself still be `approved`, and its sibling — not
    # just "some other row" — must actually be `published`.
    blocked_id = uuid.UUID(body["blocked"][0]["version_id"])
    rows = await db.execute(
        select(ContourVersion.id, ContourVersion.status).where(
            ContourVersion.import_id == approved_import_with_one_overlap.id
        )
    )
    statuses = dict(rows.all())
    assert statuses[blocked_id] == "approved"
    other_id = next(vid for vid in statuses if vid != blocked_id)
    assert statuses[other_id] == "published"
    await db.refresh(approved_import_with_one_overlap)
    assert (
        approved_import_with_one_overlap.status == "approved"
    )  # not done — something is left to fix


async def test_publishing_a_batch_requires_the_approve_permission(gis_client, approved_import):
    resp = await gis_client.post(f"/api/v1/gis/imports/{approved_import.id}/publish")
    assert resp.status_code == 403


async def test_a_non_contour_batch_is_refused_at_submit_review(
    gis_client, processed_restrictions_import
):
    """Review finding 1: `restrictions`/`fire_bans`/etc. batches carry no
    `ContourVersion` rows for the three batch actions to loop over, so without
    this guard they would ride the contour machinery straight to a false
    `done` with `{"published": 0, "blocked": []}` — indistinguishable from a
    real, empty-but-successful batch. Refused loudly instead; the created
    `layer_features` rows still publish one at a time through Task 6's own
    `POST /layers/{code}/features/{id}/publish` (no bulk path for them yet —
    a known, named gap, not silently swallowed here)."""
    resp = await gis_client.post(
        f"/api/v1/gis/imports/{processed_restrictions_import.id}/submit-review"
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["reason"] == "not_a_contour_batch"


async def test_a_non_contour_batch_is_refused_at_approve_and_publish_too(
    rahbar_client, processed_restrictions_import
):
    """Re-review: the layer guard has to live somewhere EVERY entry point
    passes through. `CONTOURS_APPROVE` is a DIFFERENT permission from
    `CONTOURS_MANAGE` — `rahbar_client` cannot call `/submit-review` at all
    (403), so it can reach `/approve` and `/publish` directly on a batch
    that just finished parsing, never having gone through submit-review.
    Both must still be refused, not silently loop zero versions to a false
    `done`."""
    iid = processed_restrictions_import.id
    approve_resp = await rahbar_client.post(f"/api/v1/gis/imports/{iid}/approve")
    assert approve_resp.status_code == 409
    assert approve_resp.json()["error"]["details"]["reason"] == "not_a_contour_batch"

    publish_resp = await rahbar_client.post(f"/api/v1/gis/imports/{iid}/publish")
    assert publish_resp.status_code == 409
    assert publish_resp.json()["error"]["details"]["reason"] == "not_a_contour_batch"


async def test_approving_a_batch_that_skipped_submit_review_is_refused(
    rahbar_client, processed_import
):
    """Re-review's contour-batch sibling: `processed_import`'s versions are
    still `draft` (nobody called `/submit-review`), so the `review`-status
    query `/approve` loops over finds nothing — a transition that would move
    zero versions must be refused, not silently advance `gis_imports.status`
    to `approved` having approved nothing."""
    resp = await rahbar_client.post(f"/api/v1/gis/imports/{processed_import.id}/approve")
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["reason"] == "empty_batch"


async def test_republishing_a_batch_whose_versions_all_published_reports_done(
    rahbar_client, db, approved_import
):
    """`publish_import` is documented as safe to call again — but a second call
    found no `approved` versions and answered 409 `empty_batch`, for a batch
    that had in fact completed. This is the corollary of the empty-batch
    refusal, and it belongs to `publish_import` alone: in `submit-review` or
    `approve` the same shortcut would advance the row two states from an action
    that moved nothing."""
    first = await rahbar_client.post(f"/api/v1/gis/imports/{approved_import.id}/publish")
    assert first.status_code == 200
    assert first.json() == {"published": 2, "blocked": []}
    await db.refresh(approved_import)
    assert approved_import.status == "done"

    # ...and a batch left at `approved` whose versions all published anyway —
    # the operator's last fix going through the single-version route.
    approved_import.status = "approved"
    await db.commit()

    again = await rahbar_client.post(f"/api/v1/gis/imports/{approved_import.id}/publish")
    assert again.status_code == 200, again.text
    assert again.json() == {"published": 0, "blocked": []}
    await db.refresh(approved_import)
    assert approved_import.status == "done"


async def test_a_batch_with_no_versions_at_all_is_still_an_empty_batch(
    rahbar_client, db, approved_import
):
    """The refusal this corollary must not swallow: `empty_batch` still names
    the real defect — a batch that never had versions."""
    await db.execute(delete(ContourVersion).where(ContourVersion.import_id == approved_import.id))
    await db.commit()

    resp = await rahbar_client.post(f"/api/v1/gis/imports/{approved_import.id}/publish")
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["reason"] == "empty_batch"


async def test_submitting_an_already_submitted_batch_says_so(gis_client, processed_import):
    """Repeated `/submit-review` keeps its 409 — the action really did nothing
    and nothing may advance — but `empty_batch` was the wrong word for an
    operator double-click on a batch already at `review` or beyond."""
    first = await gis_client.post(f"/api/v1/gis/imports/{processed_import.id}/submit-review")
    assert first.status_code == 200

    second = await gis_client.post(f"/api/v1/gis/imports/{processed_import.id}/submit-review")
    assert second.status_code == 409
    assert second.json()["error"]["details"]["reason"] == "already_submitted"
