"""`POST /applications/{id}/clone` — a fresh DRAFT pre-filled from an
application the caller owns (task 6, 3.9b): a herder renewing next season's
grazing should not have to retype the contour, the herd or the activity.

The whole point of a clone is what it does NOT copy — a copied signature, a
copied public number, a copied calculation or a copied decision would each be
a distinct defect — so these tests assert both halves: what is pre-filled and
what comes back empty."""

from tests.modules.applications.test_submit import _submit


async def test_a_clone_copies_the_request_and_nothing_that_was_earned(
    db, applicant_client, submitted_application
) -> None:
    import uuid

    from app.modules.applications.models import Application

    source = (await applicant_client.get(f"/api/v1/applications/{submitted_application}")).json()
    # Preconditions: every field the clone must NOT carry over is actually
    # SET on the source, real submission having earned it — otherwise an
    # assertion that the clone lacks it would pass vacuously even if the
    # field were copied.
    assert source["contour_version_id"] is not None
    assert source["assigned_org_id"] is not None
    assert source["assigned_user_id"] is not None
    assert source["submitted_at"] is not None

    result = await applicant_client.post(f"/api/v1/applications/{submitted_application}/clone")
    assert result.status_code == 201, result.text
    clone = result.json()

    assert clone["status"] == "DRAFT"
    assert clone["number"] is None
    assert clone["contour_id"] == source["contour_id"]
    assert clone["activity_type_id"] == source["activity_type_id"]
    assert clone["parent_application_id"] == submitted_application
    assert clone["kind"] == "new"
    # Value equality, not merely "present" — a wrong-but-FK-valid substitution
    # (someone else's applicant, a different benefit category) would slip
    # through a non-null check but not this one.
    assert clone["applicant_id"] == source["applicant_id"]
    assert clone["representation_id"] == source["representation_id"]
    assert clone["requested_area_ha"] == source["requested_area_ha"]
    assert clone["quantity"] == source["quantity"]
    assert clone["benefit_category_item_id"] == source["benefit_category_item_id"]
    # The frozen pair (ruling 22) is NOT inherited: the clone reprices
    # against whatever version is published at its OWN submission.
    assert clone["contour_version_id"] is None
    # Neither is the assignment, the SLA clock's own anchor, or the fact of
    # having been filed before.
    assert clone["assigned_org_id"] is None
    assert clone["assigned_user_id"] is None
    assert clone["submitted_at"] is None
    assert clone["decided_at"] is None

    card = (await applicant_client.get(f"/api/v1/applications/{clone['id']}")).json()
    assert card["documents"] == [], "a vet certificate expires; copying it hides that"
    assert card["checks"] == []
    assert card["calculation"] is None
    assert card["sla_deadline_at"] is None

    timeline = (await applicant_client.get(f"/api/v1/applications/{clone['id']}/timeline")).json()
    assert [row["to_status"] for row in timeline["status_history"]] == ["DRAFT"], (
        "the clone's own timeline, not the source's — the status history is not copied"
    )
    assert timeline["assignments"] == []
    assert timeline["signatures"] == []

    # Nothing sets these two before a decision, so this is a guard against a
    # future copy-everything refactor rather than a live bug today. Neither
    # is on `ApplicationOut` (`decision_basis` isn't serialized at all), so
    # the clone's own row is read directly.
    assert clone["rejection_reason_item_id"] is None
    row = await db.get(Application, uuid.UUID(clone["id"]))
    assert row is not None
    assert row.decision_basis is None


async def test_a_clone_copies_the_livestock_items(applicant_client, submitted_application) -> None:
    source = (await applicant_client.get(f"/api/v1/applications/{submitted_application}")).json()
    clone_id = (
        await applicant_client.post(f"/api/v1/applications/{submitted_application}/clone")
    ).json()["id"]

    clone = (await applicant_client.get(f"/api/v1/applications/{clone_id}")).json()
    assert [(i["livestock_type_id"], i["head_count"]) for i in clone["items"]] == [
        (i["livestock_type_id"], i["head_count"]) for i in source["items"]
    ]


async def test_cloning_someone_elses_application_is_refused(
    other_applicant_client, submitted_application
) -> None:
    result = await other_applicant_client.post(
        f"/api/v1/applications/{submitted_application}/clone"
    )
    assert result.status_code == 404


async def test_a_clone_carries_the_sources_own_representation_not_just_any_non_null_one(
    db, representative_client, legal_applicant
) -> None:
    """`representation_id` names WHICH power of attorney a filing was made
    under (`test_draft_api.py::
    test_a_representative_files_for_the_legal_entity_they_represent`'s own
    words) — a swapped or stale one would attach one person's authority to a
    filing they never gave it for, the same class of defect as a copied
    signature. `clone()` accepts a source in ANY status, so a bare DRAFT is
    enough; no submission is needed to exercise this path."""
    from sqlalchemy import select

    from app.modules.auth.models import Representation

    source = await representative_client.post(
        "/api/v1/applications",
        json={"on_behalf": "legal", "applicant_id": str(legal_applicant.id)},
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]
    representation_id = await db.scalar(
        select(Representation.id).where(Representation.applicant_id == legal_applicant.id)
    )

    clone = await representative_client.post(f"/api/v1/applications/{source_id}/clone")
    assert clone.status_code == 201, clone.text
    body = clone.json()
    assert body["applicant_id"] == str(legal_applicant.id)
    assert body["on_behalf"] == "legal"
    assert body["representation_id"] == str(representation_id)


async def test_a_clone_can_be_submitted_once_the_original_is_out_of_the_way(
    applicant_client, submitted_application
) -> None:
    """The duplicate constraint counts the ORIGINAL as active, so an immediate
    submission of the clone must be refused — and must succeed after the
    original is cancelled. This is the applicant-visible behaviour of tz/05
    invariant 1, and it is easy to get wrong by excluding clones from it."""
    clone_id = (
        await applicant_client.post(f"/api/v1/applications/{submitted_application}/clone")
    ).json()["id"]

    blocked = await _submit(applicant_client, clone_id)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "ERR-APP-002"

    await applicant_client.post(f"/api/v1/applications/{submitted_application}/cancel", json={})
    assert (await _submit(applicant_client, clone_id)).status_code == 200
