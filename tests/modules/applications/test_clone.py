"""`GET /applications/{id}/clone` — the filing a caller would send to refile
an application they own (task 6, 3.9b; stage 12 turned the write into a
read, plan 12 R6): a herder renewing next season's grazing should not have
to retype the contour, the herd or the activity.

The whole point of a clone is what it does NOT copy — a copied signature, a
copied public number, a copied calculation or a copied decision would each be
a distinct defect — so these tests assert both halves: what is pre-filled and
what comes back absent."""

from sqlalchemy import func, select

from app.modules.applications.models import Application
from tests.modules.applications.test_submit import _submit, _submit_with_button


async def test_a_clone_copies_the_request_and_nothing_that_was_earned(
    db, applicant_client, submitted_application
) -> None:
    source = (await applicant_client.get(f"/api/v1/applications/{submitted_application}")).json()
    # Preconditions: every field the clone must NOT carry over is actually
    # SET on the source, real submission having earned it — otherwise an
    # assertion that the clone lacks it would pass vacuously even if the
    # field were copied.
    assert source["contour_version_id"] is not None
    assert source["assigned_org_id"] is not None
    assert source["submitted_at"] is not None
    assert source["number"] is not None
    rows_before = await db.scalar(select(func.count()).select_from(Application))

    result = await applicant_client.get(f"/api/v1/applications/{submitted_application}/clone")
    assert result.status_code == 200, result.text
    clone = result.json()
    # The request itself, by VALUE — a wrong-but-FK-valid substitution
    # (someone else's applicant, a different benefit category) would slip
    # through a non-null check but not this one.
    assert clone["on_behalf"] == source["on_behalf"]
    assert clone["applicant_id"] == source["applicant_id"]
    assert clone["contour_id"] == source["contour_id"]
    assert clone["activity_type_id"] == source["activity_type_id"]
    assert clone["period_from"] == source["period_from"]
    assert clone["period_to"] == source["period_to"]
    assert clone["quantity"] == source["quantity"]
    assert clone["benefit_category_item_id"] == source["benefit_category_item_id"]
    # Nothing that was EARNED is in the template at all — the shape is a
    # filing's, which has no such fields to carry.
    for earned in (
        "id",
        "number",
        "status",
        "contour_version_id",
        "requested_area_ha",
        "assigned_org_id",
        "submitted_at",
        "sla_deadline_at",
        "decided_at",
        "representation_id",
    ):
        assert earned not in clone, earned
    assert clone["documents"] == [], "a vet certificate expires; copying it hides that"
    # A read: no row was created.
    assert await db.scalar(select(func.count()).select_from(Application)) == rows_before


async def test_a_clone_copies_the_livestock_items(applicant_client, submitted_application) -> None:
    source = (await applicant_client.get(f"/api/v1/applications/{submitted_application}")).json()
    clone = (
        await applicant_client.get(f"/api/v1/applications/{submitted_application}/clone")
    ).json()
    assert [(i["livestock_type_id"], i["head_count"]) for i in clone["items"]] == [
        (i["livestock_type_id"], i["head_count"]) for i in source["items"]
    ]


async def test_cloning_someone_elses_application_is_refused(
    other_applicant_client, submitted_application
) -> None:
    result = await other_applicant_client.get(f"/api/v1/applications/{submitted_application}/clone")
    assert result.status_code == 404


async def test_the_clone_route_is_a_read(applicant_client, submitted_application) -> None:
    """Stage 12: the old `POST` is gone with the draft it created."""
    result = await applicant_client.post(f"/api/v1/applications/{submitted_application}/clone")
    assert result.status_code == 405, result.text


async def test_a_clone_names_the_legal_entity_the_source_was_filed_for(
    representative_client, legal_applicant, legal_filing_ready_for_submission
) -> None:
    """The template names the same applicant and authority shape the source
    was filed under; the representation itself is resolved afresh at the
    refiling (`_resolve_applicant`), never copied — a lapsed power of
    attorney must not ride along."""
    source = await _submit(representative_client, legal_filing_ready_for_submission)
    assert source.status_code == 201, source.text
    clone = await representative_client.get(f"/api/v1/applications/{source.json()['id']}/clone")
    assert clone.status_code == 200, clone.text
    assert clone.json()["applicant_id"] == str(legal_applicant.id)
    assert clone.json()["on_behalf"] == "legal"


async def test_a_clone_can_be_filed_once_the_original_is_out_of_the_way(
    applicant_client, submitted_application
) -> None:
    """The duplicate constraint counts the ORIGINAL as active, so filing the
    template as it is must be refused — and must succeed after the original
    is cancelled. This is the applicant-visible behaviour of tz/05 invariant
    1, and it is easy to get wrong by excluding clones from it."""
    template = (
        await applicant_client.get(f"/api/v1/applications/{submitted_application}/clone")
    ).json()
    blocked = await _submit_with_button(applicant_client, template)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "ERR-APP-002"

    await applicant_client.post(f"/api/v1/applications/{submitted_application}/cancel", json={})
    refiled = await _submit_with_button(applicant_client, template)
    assert refiled.status_code == 201, refiled.text
