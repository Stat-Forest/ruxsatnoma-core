"""The application's read routes and the per-id edit routes a RETURNED
application keeps (plan 03.9a task 3, re-based on stage 12 — plan 12, R5:
there is no draft; `PATCH` serves `RETURNED` alone).

Every test here drives real HTTP through the fixtures in this package's
`conftest.py` — this is the module's first HTTP surface, so the three plumbing
pieces an HTTP-driven package needs (`_app_on_test_db`, the commit hook, the
re-exported gis fixtures) live there rather than in any one file.
"""

import uuid
from datetime import date

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications.models import Application
from tests.modules.applications.test_submit import _submit, _submit_with_button


async def _returned(db: AsyncSession, application_id: str) -> None:
    """Put a filed application into RETURNED by writing the status directly —
    the stand-in `test_the_owner_may_patch_a_returned_application` has used
    since 3.9b (its docstring says why); `test_return.py` drives the real
    route."""
    await db.execute(
        update(Application)
        .where(Application.id == uuid.UUID(application_id))
        .values(status="RETURNED")
    )
    await db.commit()


async def test_another_applicant_cannot_read_my_application(
    other_applicant_client, submitted_application
) -> None:
    seen = await other_applicant_client.get(f"/api/v1/applications/{submitted_application}")
    assert seen.status_code == 404, "ownership leaks are 404, not 403 — do not confirm it exists"


async def test_a_hodim_outside_the_zone_does_not_see_the_application(
    submitted_application: str,
    hodim_client,
    other_zone_hodim_client,
) -> None:
    """The zone half of the read rule, on a SUBMITTED application."""
    app_id = submitted_application
    assert (await hodim_client.get(f"/api/v1/applications/{app_id}")).status_code == 200
    assert (await other_zone_hodim_client.get(f"/api/v1/applications/{app_id}")).status_code == 404


async def test_the_card_carries_the_keys_every_later_task_reads(
    applicant_client, submitted_application
) -> None:
    """`checks` and `calculation` are on the card and, since stage 12, filled
    by the filing itself: `payments`' and `permits`' own tests read
    `card["calculation"]["amount"]`, and a key that appears halfway through a
    stage is a key a front end has to learn twice."""
    card = (await applicant_client.get(f"/api/v1/applications/{submitted_application}")).json()
    assert card["status"] == "SUBMITTED"
    assert card["checks"], "the filing recorded its checks"
    assert card["calculation"]["amount"]
    assert card["documents"] == []
    assert [item["head_count"] for item in card["items"]] == [40]
    assert date.fromisoformat(card["period_from"]) == date(2027, 5, 1)


async def test_items_are_replaced_wholesale_never_merged(
    db, applicant_client, submitted_application
) -> None:
    """An applicant removing a livestock kind must be able to; merge semantics
    would make that impossible (the brief's own words). On RETURNED — the one
    status PATCH serves since stage 12."""
    await _returned(db, submitted_application)
    emptied = await applicant_client.patch(
        f"/api/v1/applications/{submitted_application}", json={"items": []}
    )
    assert emptied.status_code == 200, emptied.text
    card = (await applicant_client.get(f"/api/v1/applications/{submitted_application}")).json()
    assert card["items"] == []


async def test_requested_area_ha_is_not_patchable(
    db, applicant_client, submitted_application
) -> None:
    """Ruling 22 freezes it at submission from the contour version's own
    `area_ha`. It is refused as an UNKNOWN field, not silently ignored — a
    client that thinks it set the area must be told it did not."""
    await _returned(db, submitted_application)
    refused = await applicant_client.patch(
        f"/api/v1/applications/{submitted_application}", json={"requested_area_ha": "12.5"}
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "ERR-VAL-001"


async def test_the_list_shows_my_own_applications_and_not_a_strangers(
    applicant_client, other_applicant_client, filing_ready_for_submission
) -> None:
    """The list is scoped by identity, never by a filter the caller supplies:
    an applicant sees their own rows and a stranger's are simply absent."""
    mine = await _submit_with_button(applicant_client, filing_ready_for_submission)
    assert mine.status_code == 201, mine.text
    theirs = await _submit_with_button(other_applicant_client, filing_ready_for_submission)
    assert theirs.status_code == 201, theirs.text
    listed = await applicant_client.get("/api/v1/applications", params={"status": "SUBMITTED"})
    assert listed.status_code == 200
    ids = [row["id"] for row in listed.json()["items"]]
    assert mine.json()["id"] in ids
    assert theirs.json()["id"] not in ids


async def test_the_list_is_zoned_for_staff_before_anyone_is_assigned(
    submitted_application: str,
    published_contour,
    hodim_client,
    other_zone_hodim_client,
) -> None:
    """`assigned_org_id` is null until `start-review` writes the assignment
    (ruling 14), so the zone rule has to reach the CONTOUR's owner — otherwise a
    hodim's work queue is empty of exactly the applications they are supposed to
    pick up. The card next door resolves that per row; this proves the paged
    query resolves it the same way.

    **SUBMITTED, not DRAFT** (ruling #110): a DRAFT is invisible to staff
    everywhere now, list included — `list_applications`'s own "can never
    disagree with the card" promise excludes `INITIAL_STATUS` from the staff
    scope for exactly that reason. "Before anyone is assigned" therefore
    starts at SUBMITTED here: auto-assignment only ever runs inside
    `start_review`, never at submission itself, so `submitted_application` is
    still genuinely unassigned.
    """
    app_id = submitted_application

    mine = await hodim_client.get(
        "/api/v1/applications", params={"contour_id": str(published_contour.id)}
    )
    assert mine.status_code == 200, mine.text
    assert [row["id"] for row in mine.json()["items"]] == [app_id]

    theirs = await other_zone_hodim_client.get(
        "/api/v1/applications", params={"contour_id": str(published_contour.id)}
    )
    assert theirs.status_code == 200
    assert theirs.json()["items"] == []
    assert theirs.json()["total"] == 0


async def test_a_stranger_cannot_patch_my_application(
    other_applicant_client, submitted_application
) -> None:
    """404, not 403: a refusal that confirmed the id was an application would
    make PATCH the existence oracle the card refuses to be."""
    refused = await other_applicant_client.patch(
        f"/api/v1/applications/{submitted_application}", json={"period_from": "2027-05-01"}
    )
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "ERR-SYS-003"


async def test_an_application_in_flight_can_no_longer_be_patched(
    applicant_client, submitted_application
) -> None:
    """409 `ERR-APP-004` in any status but RETURNED (plan 12, R5): a filed
    application is the leshoz's to read, not the applicant's to edit."""
    refused = await applicant_client.patch(
        f"/api/v1/applications/{submitted_application}", json={"period_from": "2027-05-01"}
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "ERR-APP-004"
    assert refused.json()["error"]["details"]["status"] == "SUBMITTED"


async def test_the_owner_may_patch_a_returned_application(
    db, applicant_client, submitted_application
) -> None:
    """`service._EDITABLE_STATUSES` holds `RETURNED` (3.9b task 1 review,
    Important finding; stage 12 removed DRAFT beside it): an application is
    returned for correction precisely so the applicant can correct it. The
    assertion reads the change back through the response body, not merely a
    200 — a route that silently no-ops on a RETURNED application would answer
    200 too."""
    await _returned(db, submitted_application)
    patched = await applicant_client.patch(
        f"/api/v1/applications/{submitted_application}", json={"period_to": "2027-09-20"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["period_to"] == "2027-09-20"
    card = (await applicant_client.get(f"/api/v1/applications/{submitted_application}")).json()
    assert card["period_to"] == "2027-09-20", "the PATCH must actually land, not just answer 200"


async def test_a_stranger_still_cannot_patch_a_returned_application(
    db, applicant_client, other_applicant_client, submitted_application
) -> None:
    """RETURNED being editable must not also make it readable/writable by
    anyone but its owner — the SAME 404 `ERR-SYS-003` a stranger gets on an
    application in flight."""
    await _returned(db, submitted_application)
    refused = await other_applicant_client.patch(
        f"/api/v1/applications/{submitted_application}", json={"period_to": "2027-09-20"}
    )
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_representative_files_for_the_legal_entity_they_represent(
    db, representative_client, legal_applicant, legal_filing_ready_for_submission
) -> None:
    """`on_behalf="legal"` (decision #9: a legal entity has no account of its
    own). The assertion that matters is `representation_id`: it records WHICH
    power of attorney the filing was made under, and it is the legal basis of
    the application — an application filed for a company by nobody in
    particular is not a document anyone can stand behind."""
    from sqlalchemy import select

    from app.modules.auth.models import Representation

    created = await _submit(representative_client, legal_filing_ready_for_submission)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["applicant_id"] == str(legal_applicant.id)
    assert body["on_behalf"] == "legal"
    representation_id = await db.scalar(
        select(Representation.id).where(Representation.applicant_id == legal_applicant.id)
    )
    assert body["representation_id"] == str(representation_id), (
        "the stored representation is the one the caller actually holds, not just any non-null id"
    )


async def test_filing_for_a_legal_entity_you_do_not_represent_is_refused(
    applicant_client, legal_applicant
) -> None:
    """403 `ERR-ACL-001`, not 404: the caller NAMED the applicant, so there is
    no existence to hide — and unlike an application, an `applicants` row for a
    company is public information (its STIR is on every invoice it issues).
    Without this guard anyone could file in any company's name."""
    refused = await _submit_with_button(
        applicant_client, {"on_behalf": "legal", "applicant_id": str(legal_applicant.id)}
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "ERR-ACL-001"
    assert refused.json()["error"]["details"]["reason"] == "no_effective_representation"


async def test_on_behalf_legal_needs_an_applicant_id(applicant_client) -> None:
    """`applicant_id` is optional in the schema because `on_behalf="self"` must
    not need it — so the pairing rule is the service's, and it says so."""
    refused = await _submit_with_button(applicant_client, {"on_behalf": "legal"})
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["reason"] == "applicant_id_required"


async def test_naming_someone_elses_applicant_on_behalf_of_self_is_refused(
    applicant_client, legal_applicant
) -> None:
    """Refused rather than IGNORED: silently overriding the field is how a
    client ends up believing it filed for the person it named."""
    refused = await _submit_with_button(
        applicant_client, {"on_behalf": "self", "applicant_id": str(legal_applicant.id)}
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["reason"] == "applicant_is_not_the_caller"


async def test_an_unknown_reference_id_is_a_422_and_not_a_500(
    applicant_client, sheep_type_id
) -> None:
    """`_assert_references`' whole purpose: an unknown FK reaching `flush()` is
    an `IntegrityError`, which has no handler in `app/main.py` and surfaces as
    `ERR-SYS-001`/500 for what is only ever a typo. One case per guard, each
    asserting its OWN reason — an outcome-only test could not tell the four
    apart (lesson). Through the stateless pre-check, which runs the same
    `_assert_references` the filing does."""
    stranger = str(uuid.uuid4())
    for body, reason in (
        ({"activity_type_id": stranger}, "unknown_activity_type"),
        ({"contour_id": stranger}, "unknown_contour"),
        ({"benefit_category_item_id": stranger}, "unknown_benefit_category"),
        (
            {
                "items": [
                    {"livestock_type_id": str(sheep_type_id), "head_count": 10},
                    {"livestock_type_id": str(sheep_type_id), "head_count": 20},
                ]
            },
            "duplicate_livestock_type",
        ),
        ({"items": [{"livestock_type_id": stranger, "head_count": 10}]}, "unknown_livestock_type"),
    ):
        refused = await applicant_client.post(
            "/api/v1/applications/precheck", json={"on_behalf": "self", **body}
        )
        assert refused.status_code == 422, (body, refused.text)
        assert refused.json()["error"]["code"] == "ERR-VAL-001"
        assert refused.json()["error"]["details"]["reason"] == reason


async def test_a_benefit_item_from_another_classifier_is_refused(db, applicant_client) -> None:
    """The membership half of the benefit guard: an id that IS a real
    `classifier_items` row but belongs to the rejection-reason classifier must
    not pass as a benefit category. An existence-only check would let it."""
    from sqlalchemy import text as sa_text

    rejection_item_id = await db.scalar(
        sa_text(
            "SELECT ci.id FROM classifier_items ci JOIN classifiers c ON c.id = ci.classifier_id"
            " WHERE c.code = 'rejection_reasons' LIMIT 1"
        )
    )
    refused = await applicant_client.post(
        "/api/v1/applications/precheck",
        json={"on_behalf": "self", "benefit_category_item_id": str(rejection_item_id)},
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["reason"] == "unknown_benefit_category"


async def test_the_audit_trail_records_the_herd_that_changed(
    db, applicant_client, submitted_application, sheep_type_id
) -> None:
    """Review I1: `items` is the field on this table that drives the fee, the
    norm check and the printed permit, so an `application.update` row that
    cannot show it changed is worse than no row — a `prosecutor` reading
    `audit_log` would be told nothing happened."""
    from sqlalchemy import select

    from app.modules.audit.models import AuditLog

    await _returned(db, submitted_application)
    patched = await applicant_client.patch(
        f"/api/v1/applications/{submitted_application}",
        json={"items": [{"livestock_type_id": str(sheep_type_id), "head_count": 4000}]},
    )
    assert patched.status_code == 200, patched.text
    entries = list(
        (
            await db.execute(
                select(AuditLog)
                .where(
                    AuditLog.action == "application.update",
                    AuditLog.object_id == uuid.UUID(submitted_application),
                )
                .order_by(AuditLog.id)
            )
        ).scalars()
    )
    assert len(entries) == 1
    assert [line["head_count"] for line in entries[0].old_value["items"]] == [40]
    assert [line["head_count"] for line in entries[0].new_value["items"]] == [4000]


async def test_moving_a_returned_application_to_another_contour_clears_the_frozen_version(
    db: AsyncSession,
    applicant_client,
    submitted_application: str,
    contours_layer,
    leshoz,
    approval_doc,
) -> None:
    """**A filed application carries a frozen version, and a later PATCH on
    RETURNED must not let it outlive its contour** (3.9a final review, re-based
    on stage 12).

    Step 4 froze `contour_version_id` and `requested_area_ha` at filing. What
    must not survive is the pair naming a plot the application no longer
    points at — `max_approve_area` (decision #29) is compared against
    `requested_area_ha`, and a permit reads `contour_version_id` straight.
    """
    from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt

    row = await db.get(Application, uuid.UUID(submitted_application))
    assert row is not None
    assert row.contour_version_id is not None, "the filing froze the pair"
    assert row.requested_area_ha is not None

    await _returned(db, submitted_application)
    elsewhere = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db, elsewhere.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    await db.flush()
    await db.commit()
    patched = await applicant_client.patch(
        f"/api/v1/applications/{submitted_application}", json={"contour_id": str(elsewhere.id)}
    )
    assert patched.status_code == 200, patched.text
    await db.refresh(row)
    assert row.contour_id == elsewhere.id
    assert row.contour_version_id is None, "a version of the OLD contour cannot survive the move"
    assert row.requested_area_ha is None


async def test_the_list_puts_the_most_recently_updated_application_first(
    db, applicant_client, filing_ready_for_submission, another_ready_filing, sheep_type_id
) -> None:
    """The queue is ordered by `updated_at DESC`, not by creation (#201): an
    older application that was just touched must climb above a newer
    untouched one, or a reviewer's "what changed since I looked" reading of
    the list is silently wrong. `id` (uuid7, creation-ordered) stays as the
    tie-break only. Touched through the one edit route a filed application
    keeps — PATCH on RETURNED (stage 12)."""
    older = await _submit_with_button(applicant_client, filing_ready_for_submission)
    newer = await _submit_with_button(applicant_client, another_ready_filing)
    assert older.status_code == 201 and newer.status_code == 201, (older.text, newer.text)

    await _returned(db, older.json()["id"])
    touched = await applicant_client.patch(
        f"/api/v1/applications/{older.json()['id']}",
        json={"items": [{"livestock_type_id": str(sheep_type_id), "head_count": 41}]},
    )
    assert touched.status_code == 200, touched.text

    listed = await applicant_client.get("/api/v1/applications")
    assert listed.status_code == 200
    ids = [row["id"] for row in listed.json()["items"]]
    assert ids == [older.json()["id"], newer.json()["id"]]


async def test_q_finds_an_application_by_its_number_and_by_the_applicants_name(
    db, applicant_client, applicant, filing_ready_for_submission
) -> None:
    """`q` is the list's one free-text filter (the former `/search` screen
    folded into it): a case-insensitive substring of the number or of the
    applicant's name, and nothing for a name nobody carries."""
    applicant.name = "Каримов Карим Каримович"
    await db.commit()
    mine = await _submit_with_button(applicant_client, filing_ready_for_submission)
    assert mine.status_code == 201, mine.text
    number = mine.json()["number"]

    by_name = await applicant_client.get("/api/v1/applications", params={"q": "каримов"})
    assert by_name.status_code == 200, by_name.text
    assert [row["id"] for row in by_name.json()["items"]] == [mine.json()["id"]]

    by_number = await applicant_client.get("/api/v1/applications", params={"q": number[-4:]})
    assert [row["id"] for row in by_number.json()["items"]] == [mine.json()["id"]]

    nobody = await applicant_client.get("/api/v1/applications", params={"q": "Азизов"})
    assert nobody.json()["total"] == 0
