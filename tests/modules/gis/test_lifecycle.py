"""tz/07's lifecycle: Draft → Review → Approved → Published → Archived. Publishing
runs the checks (Task 4) and archives the version it replaces, so exactly one
version is ever in force — and an already-issued permit keeps pointing at its own."""


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
