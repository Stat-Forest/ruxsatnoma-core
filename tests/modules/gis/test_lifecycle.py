"""tz/07's lifecycle: Draft → Review → Approved → Published → Archived. Publishing
runs the checks (Task 4) and archives the version it replaces, so exactly one
version is ever in force — and an already-issued permit keeps pointing at its own."""

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.modules.gis.models import ContourVersion


async def test_the_happy_path_publishes_and_archives_the_previous_version(
    gis_client, rahbar_client, contour_with_draft, approval_doc
):
    cid, vid = contour_with_draft
    assert (
        await gis_client.post(f"/api/v1/gis/contours/{cid}/versions/{vid}/submit-review")
    ).status_code == 200
    approved = await rahbar_client.post(
        f"/api/v1/gis/contours/{cid}/versions/{vid}/approve",
        json={"approval_doc_id": str(approval_doc.id)},
    )
    assert approved.status_code == 200
    published = await rahbar_client.post(f"/api/v1/gis/contours/{cid}/versions/{vid}/publish")
    assert published.status_code == 200
    assert published.json()["status"] == "published"


async def test_approve_requires_the_approve_permission(gis_client, contour_in_review, approval_doc):
    """The GIS specialist may not approve their own work — that is the rahbar's."""
    cid, vid = contour_in_review
    resp = await gis_client.post(
        f"/api/v1/gis/contours/{cid}/versions/{vid}/approve",
        json={"approval_doc_id": str(approval_doc.id)},
    )
    assert resp.status_code == 403


async def test_publishing_a_draft_directly_is_refused(rahbar_client, contour_with_draft):
    cid, vid = contour_with_draft
    resp = await rahbar_client.post(f"/api/v1/gis/contours/{cid}/versions/{vid}/publish")
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["reason"] == "bad_transition"


async def test_publishing_an_overlapping_version_is_refused_with_the_check_report(
    rahbar_client, approved_version_overlapping_a_published_one
):
    cid, vid = approved_version_overlapping_a_published_one
    resp = await rahbar_client.post(f"/api/v1/gis/contours/{cid}/versions/{vid}/publish")
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "ERR-GIS-003"
    assert any(c["check"] == "overlap" and c["result"] == "fail" for c in body["details"]["checks"])


async def test_publishing_a_second_version_archives_the_first(
    rahbar_client, db, contour_with_two_versions
):
    """The partial unique index would raise otherwise — the archive must happen in
    the same transaction, before the insert of the new published state."""
    cid, first_vid, second_vid = contour_with_two_versions
    resp = await rahbar_client.post(f"/api/v1/gis/contours/{cid}/versions/{second_vid}/publish")
    assert resp.status_code == 200
    from app.modules.gis.models import ContourVersion

    first = await db.get(ContourVersion, first_vid)
    assert first is not None and first.status == "archived"


async def test_approve_without_a_document_is_422(rahbar_client, contour_in_review):
    cid, vid = contour_in_review
    resp = await rahbar_client.post(f"/api/v1/gis/contours/{cid}/versions/{vid}/approve", json={})
    assert resp.status_code == 422


async def test_approve_is_refused_outside_the_actors_zone(
    org_scoped_rahbar_client, contour_in_review, approval_doc
):
    """Task-5 controller, decision 6: every one of the four lifecycle actions is
    zone-scoped, not only permission-gated (lesson: 'Zone scoping is not a
    permission check — a read path needs both') — `contour_in_review` sits
    under `leshoz`, and this actor is zoned to a DIFFERENT organization
    (`other_leshoz`), so even holding CONTOURS_APPROVE must not be enough."""
    cid, vid = contour_in_review
    resp = await org_scoped_rahbar_client.post(
        f"/api/v1/gis/contours/{cid}/versions/{vid}/approve",
        json={"approval_doc_id": str(approval_doc.id)},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"


async def test_a_region_scoped_approver_cannot_publish_into_another_region(
    other_region_rahbar_client, leshoz_in_fergana, contour_with_two_versions
):
    """The final-review hole, mirrored from
    `test_a_region_scoped_actor_cannot_create_a_republic_wide_feature`: an
    actor holding a REGION but no organization used to pass `_assert_in_zone`
    for every organization in the country, because it compared
    `zone.organization_id` alone. `contour_with_two_versions` sits under a
    leshoz in fergana; this actor is scoped to andijan and must be refused
    even though they hold `CONTOURS_APPROVE`."""
    cid, _first_vid, second_vid = contour_with_two_versions
    resp = await other_region_rahbar_client.post(
        f"/api/v1/gis/contours/{cid}/versions/{second_vid}/publish"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"


async def test_a_blocked_version_is_sent_back_edited_and_published(
    rahbar_client, gis_client, db, approved_version_overlapping_a_published_one, approval_doc
):
    """`TRANSITIONS` declared `approved`->`review` and `review`->`draft` from
    task 5 on, but no route drove either — so a version a publish check blocked
    was stuck at `approved` forever: `update_version` refuses anything but
    `draft` and `archive_version` requires `published`, while `publish_import`'s
    own docstring and design/03 both promise the operator can fix it and re-run.
    This walks that whole promised loop."""
    cid, vid = approved_version_overlapping_a_published_one
    base = f"/api/v1/gis/contours/{cid}/versions/{vid}"

    blocked = await rahbar_client.post(f"{base}/publish")
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "ERR-GIS-003"

    # ...and, until this fix, that was the end of the line.
    assert (await gis_client.patch(base, json={"accuracy_m": "3.0"})).status_code == 409

    assert (await rahbar_client.post(f"{base}/return-to-review")).json()["status"] == "review"
    assert (await gis_client.post(f"{base}/return-to-draft")).json()["status"] == "draft"
    assert (await gis_client.patch(base, json={"accuracy_m": "3.0"})).status_code == 200

    # The operator's real fix: take the obstruction out of force, then re-run.
    target = aliased(ContourVersion)
    obstructions = (
        await db.execute(
            select(ContourVersion.contour_id, ContourVersion.id)
            .join(target, target.id == vid)
            .where(
                ContourVersion.status == "published",
                ContourVersion.contour_id != cid,
                func.ST_Intersects(ContourVersion.geom, target.geom),
            )
        )
    ).all()
    assert obstructions, "the fixture must have published exactly what this version overlaps"
    for other_cid, other_vid in obstructions:
        archived = await rahbar_client.post(
            f"/api/v1/gis/contours/{other_cid}/versions/{other_vid}/archive"
        )
        assert archived.status_code == 200, archived.text

    assert (await gis_client.post(f"{base}/submit-review")).status_code == 200
    approved = await rahbar_client.post(
        f"{base}/approve", json={"approval_doc_id": str(approval_doc.id)}
    )
    assert approved.status_code == 200, approved.text
    published = await rahbar_client.post(f"{base}/publish")
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"


async def test_the_send_back_routes_refuse_a_transition_the_state_does_not_allow(
    rahbar_client, gis_client, contour_with_draft
):
    """Both new routes go through the same `_assert_transition` table their
    four siblings use — a draft has nothing to be sent back from."""
    cid, vid = contour_with_draft
    base = f"/api/v1/gis/contours/{cid}/versions/{vid}"
    for client, route in ((rahbar_client, "return-to-review"), (gis_client, "return-to-draft")):
        resp = await client.post(f"{base}/{route}")
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["details"]["reason"] == "bad_transition"


async def test_sending_back_needs_the_right_permission_for_each_direction(
    rahbar_client, gis_client, contour_in_review
):
    """`return-to-review` is the approver's (they take their own approval
    back); `return-to-draft` is the specialist's (they take their own
    submission back). Neither actor may drive the other's edge."""
    cid, vid = contour_in_review
    base = f"/api/v1/gis/contours/{cid}/versions/{vid}"
    assert (await gis_client.post(f"{base}/return-to-review")).status_code == 403
    assert (await rahbar_client.post(f"{base}/return-to-draft")).status_code == 403


async def test_submit_review_cannot_drive_the_rework_edge_it_shares_a_target_with(
    gis_client, contour_with_two_versions
):
    """The mirror of the leak the send-back routes were guarded against, on the
    OLDER route. `review` is the one state in `TRANSITIONS` with two sources,
    so `"review" in TRANSITIONS["approved"]` is True: a target-only check let a
    `CONTOURS_MANAGE` holder submit-review an ALREADY APPROVED version and
    drive `approved` -> `review` — `return-to-review`'s edge, which is
    `CONTOURS_APPROVE` — and audited it under the wrong action code. Pre-dated
    this wave; leaving it would have defeated the permission split the wave
    installed."""
    cid, _published_vid, approved_vid = contour_with_two_versions
    resp = await gis_client.post(
        f"/api/v1/gis/contours/{cid}/versions/{approved_vid}/submit-review"
    )
    assert resp.status_code == 409, resp.text
    details = resp.json()["error"]["details"]
    assert details["reason"] == "bad_transition"
    assert (details["from"], details["to"]) == ("approved", "review")
