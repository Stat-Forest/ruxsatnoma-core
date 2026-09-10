"""The draft's four routes: create, patch, list, card (plan 03.9a task 3).

Every test here drives real HTTP through the fixtures in this package's
`conftest.py` — this is the module's first HTTP surface, so the three plumbing
pieces an HTTP-driven package needs (`_app_on_test_db`, the commit hook, the
re-exported gis fixtures) live there rather than in any one file.
"""

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications.models import Application


async def test_a_draft_starts_empty_and_is_patched_field_by_field(
    applicant_client, published_contour, grazing_activity_id, sheep_type_id
) -> None:
    """Ruling 7: a draft is autosaved after every field, so it must be storable
    half-empty. Validation is the pre-check's job and the submission's, not the
    draft's."""
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    assert created.status_code == 201, created.text
    app_id = created.json()["id"]
    assert created.json()["status"] == "DRAFT"
    assert created.json()["contour_id"] is None

    patched = await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={"activity_type_id": str(grazing_activity_id)},
    )
    assert patched.status_code == 200
    assert patched.json()["period_from"] is None

    patched = await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={
            "contour_id": str(published_contour.id),
            "period_from": "2027-05-01",
            "period_to": "2027-09-30",
            "items": [{"livestock_type_id": str(sheep_type_id), "head_count": 40}],
        },
    )
    assert patched.status_code == 200


async def test_another_applicant_cannot_read_my_draft(
    applicant_client, other_applicant_client
) -> None:
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]

    seen = await other_applicant_client.get(f"/api/v1/applications/{app_id}")
    assert seen.status_code == 404, "ownership leaks are 404, not 403 — do not confirm it exists"


async def test_a_hodim_outside_the_zone_does_not_see_the_application(
    submitted_application: str,
    hodim_client,
    other_zone_hodim_client,
) -> None:
    """The zone half of the read rule, on a SUBMITTED application — a DRAFT
    would prove nothing about ZONE, because ruling #110 refuses staff there
    for an entirely different reason (ownership, before the zone is even
    consulted; see `test_a_draft_is_invisible_to_staff_of_any_zone` below)."""
    app_id = submitted_application

    assert (await hodim_client.get(f"/api/v1/applications/{app_id}")).status_code == 200
    assert (await other_zone_hodim_client.get(f"/api/v1/applications/{app_id}")).status_code == 404


async def test_a_draft_is_invisible_to_staff_of_any_zone(
    applicant_client,
    published_contour,
    grazing_activity_id,
    hodim_client,
    prosecutor_client,
) -> None:
    """Ruling #110 (`tz/12` #26): `GET /applications/{id}` admitted in-zone
    staff in EVERY status, DRAFT included — a draft is an unsent letter, and
    until the citizen submits, nobody in the office has business reading it.

    404, not 403 (the same existence-oracle reasoning `_readable_application`
    already states for ownership): the caller must not learn that an id names
    a real, still-unfiled application. Two staff shapes, on purpose —
    `hodim_client` is IN-ZONE (the case a bare zone check would wave through)
    and `prosecutor_client` is ZONE-FREE (the case that skips the zone check
    entirely and would otherwise read every citizen's draft nationwide);
    ruling #110 refuses both for the identical reason, checked before either
    kind of zone question is even asked.
    """
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]
    await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(grazing_activity_id),
        },
    )

    in_zone = await hodim_client.get(f"/api/v1/applications/{app_id}")
    assert in_zone.status_code == 404, "in-zone staff still may not read a DRAFT they do not own"
    assert in_zone.json()["error"]["code"] == "ERR-SYS-003"

    zone_free = await prosecutor_client.get(f"/api/v1/applications/{app_id}")
    assert zone_free.status_code == 404, "view_any is zone-free, not ownership-free"

    mine = await applicant_client.get(f"/api/v1/applications/{app_id}")
    assert mine.status_code == 200, "the owner reads their own draft throughout"

    submitted = await applicant_client.get(f"/api/v1/applications/{app_id}/timeline")
    assert submitted.status_code == 200, (
        "every _readable_application route agrees, not just the card"
    )


async def test_the_card_carries_the_keys_every_later_task_reads(
    applicant_client, published_contour, grazing_activity_id, sheep_type_id
) -> None:
    """`checks` and `calculation` are on the card from THIS task, empty and null
    (nothing writes either before task 4 and task 5). They are not additions a
    later task may make: `payments`' and `permits`' own tests already read
    `card["calculation"]["amount"]`, and a key that appears halfway through a
    stage is a key a front end has to learn twice."""
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]
    await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(grazing_activity_id),
            "period_from": "2027-05-01",
            "period_to": "2027-09-30",
            "items": [{"livestock_type_id": str(sheep_type_id), "head_count": 40}],
        },
    )

    card = (await applicant_client.get(f"/api/v1/applications/{app_id}")).json()
    assert card["status"] == "DRAFT"
    assert card["checks"] == []
    assert card["calculation"] is None
    assert card["documents"] == []
    assert [item["head_count"] for item in card["items"]] == [40]
    assert date.fromisoformat(card["period_from"]) == date(2027, 5, 1)


async def test_items_are_replaced_wholesale_never_merged(applicant_client, sheep_type_id) -> None:
    """An applicant removing a livestock kind must be able to; merge semantics
    would make that impossible (the brief's own words)."""
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]
    await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={"items": [{"livestock_type_id": str(sheep_type_id), "head_count": 40}]},
    )

    emptied = await applicant_client.patch(f"/api/v1/applications/{app_id}", json={"items": []})
    assert emptied.status_code == 200
    card = (await applicant_client.get(f"/api/v1/applications/{app_id}")).json()
    assert card["items"] == []


async def test_requested_area_ha_is_not_patchable(applicant_client) -> None:
    """Ruling 22 freezes it at submission from the contour version's own
    `area_ha`. It is refused as an UNKNOWN field, not silently ignored — a
    client that thinks it set the area must be told it did not."""
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]

    refused = await applicant_client.patch(
        f"/api/v1/applications/{app_id}", json={"requested_area_ha": "12.5"}
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "ERR-VAL-001"


async def test_the_list_shows_my_own_applications_and_not_a_strangers(
    applicant_client, other_applicant_client
) -> None:
    """The list is scoped by identity, never by a filter the caller supplies:
    an applicant sees their own rows and a stranger's are simply absent."""
    mine = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    theirs = await other_applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})

    listed = await applicant_client.get("/api/v1/applications", params={"status": "DRAFT"})
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


async def test_a_stranger_cannot_patch_my_draft(applicant_client, other_applicant_client) -> None:
    """404, not 403: a refusal that confirmed the id was an application would
    make PATCH the existence oracle the card refuses to be."""
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]

    refused = await other_applicant_client.patch(
        f"/api/v1/applications/{app_id}", json={"period_from": "2027-05-01"}
    )
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_draft_that_has_moved_on_can_no_longer_be_patched(db, applicant_client) -> None:
    """409 `ERR-APP-004` in any status but DRAFT. The precondition is built
    through the real transition — `service.set_status`, the one way an
    application's status ever moves — never by assigning `status` on the row
    (lesson: a hand-set status hides a regression in the transition itself)."""
    import uuid as _uuid

    from app.modules.applications import service

    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = _uuid.UUID(created.json()["id"])
    await service.set_status(db, app_id, to_status="CANCELLED")
    await db.commit()

    refused = await applicant_client.patch(
        f"/api/v1/applications/{app_id}", json={"period_from": "2027-05-01"}
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "ERR-APP-004"
    assert refused.json()["error"]["details"]["reason"] == "not_draft"


async def test_the_owner_may_patch_a_returned_application(
    db, applicant_client, submitted_application
) -> None:
    """`service._EDITABLE_STATUSES` deliberately holds `{DRAFT, RETURNED}`,
    not `DRAFT` alone (3.9b task 1 review, Important finding): an application
    is returned for correction precisely so the applicant can correct it, so
    PATCH must reach RETURNED exactly as it reaches DRAFT. Do NOT narrow
    `_EDITABLE_STATUSES` back to `DRAFT`-only — that would silently make a
    "return for correction" a dead end nobody can act on.

    Built by injecting the status directly, the same way
    `test_assignment.py::test_a_resubmission_does_not_re_fire_auto_assignment`
    does: Task 3's `/return` route does not exist yet. Once it ships, that
    route is what will produce RETURNED for real; this injection stands in
    for it, not a second, competing way to reach the state. The assertion
    reads the change back through the response body, not merely a 200 — a
    route that silently no-ops on a RETURNED application would answer 200
    too.
    """
    import uuid as _uuid

    from sqlalchemy import update

    from app.modules.applications.models import Application

    await db.execute(
        update(Application)
        .where(Application.id == _uuid.UUID(submitted_application))
        .values(status="RETURNED")
    )
    await db.commit()

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
    """RETURNED becoming editable again (task 1 review finding) must not also
    make it readable/writable by anyone but its owner — the SAME 404
    `ERR-SYS-003` `test_a_stranger_cannot_patch_my_draft` already gets against
    a DRAFT."""
    import uuid as _uuid

    from sqlalchemy import update

    from app.modules.applications.models import Application

    await db.execute(
        update(Application)
        .where(Application.id == _uuid.UUID(submitted_application))
        .values(status="RETURNED")
    )
    await db.commit()

    refused = await other_applicant_client.patch(
        f"/api/v1/applications/{submitted_application}", json={"period_to": "2027-09-20"}
    )
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_representative_files_for_the_legal_entity_they_represent(
    db, representative_client, legal_applicant
) -> None:
    """`on_behalf="legal"` (decision #9: a legal entity has no account of its
    own). The assertion that matters is `representation_id`: it records WHICH
    power of attorney the filing was made under, and it is the legal basis of
    the application — an application filed for a company by nobody in
    particular is not a document anyone can stand behind."""
    from sqlalchemy import select

    from app.modules.auth.models import Representation

    created = await representative_client.post(
        "/api/v1/applications",
        json={"on_behalf": "legal", "applicant_id": str(legal_applicant.id)},
    )
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
    refused = await applicant_client.post(
        "/api/v1/applications",
        json={"on_behalf": "legal", "applicant_id": str(legal_applicant.id)},
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "ERR-ACL-001"
    assert refused.json()["error"]["details"]["reason"] == "no_effective_representation"


async def test_on_behalf_legal_needs_an_applicant_id(applicant_client) -> None:
    """`applicant_id` is optional in the schema because `on_behalf="self"` must
    not need it — so the pairing rule is the service's, and it says so."""
    refused = await applicant_client.post("/api/v1/applications", json={"on_behalf": "legal"})
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["reason"] == "applicant_id_required"


async def test_naming_someone_elses_applicant_on_behalf_of_self_is_refused(
    applicant_client, legal_applicant
) -> None:
    """Refused rather than IGNORED: silently overriding the field is how a
    client ends up believing it filed for the person it named."""
    refused = await applicant_client.post(
        "/api/v1/applications",
        json={"on_behalf": "self", "applicant_id": str(legal_applicant.id)},
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
    apart (lesson)."""
    import uuid as _uuid

    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]
    stranger = str(_uuid.uuid4())

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
        refused = await applicant_client.patch(f"/api/v1/applications/{app_id}", json=body)
        assert refused.status_code == 422, (body, refused.text)
        assert refused.json()["error"]["code"] == "ERR-VAL-001"
        assert refused.json()["error"]["details"]["reason"] == reason


async def test_a_benefit_item_from_another_classifier_is_refused(db, applicant_client) -> None:
    """The membership half of the benefit guard: an id that IS a real
    `classifier_items` row but belongs to the rejection-reason classifier must
    not pass as a benefit category. An existence-only check would let it."""
    from sqlalchemy import text as sa_text

    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]
    rejection_item_id = await db.scalar(
        sa_text(
            "SELECT ci.id FROM classifier_items ci JOIN classifiers c ON c.id = ci.classifier_id"
            " WHERE c.code = 'rejection_reasons' LIMIT 1"
        )
    )

    refused = await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={"benefit_category_item_id": str(rejection_item_id)},
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["reason"] == "unknown_benefit_category"


async def test_the_audit_trail_records_the_herd_that_changed(
    db, applicant_client, sheep_type_id
) -> None:
    """Review I1: `items` is the field on this table that drives the fee, the
    norm check and the printed permit, so an `application.update` row that
    cannot show it changed is worse than no row — a `prosecutor` reading
    `audit_log` would be told nothing happened."""
    import uuid as _uuid

    from sqlalchemy import select

    from app.modules.audit.models import AuditLog

    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]
    await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={"items": [{"livestock_type_id": str(sheep_type_id), "head_count": 40}]},
    )
    await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={"items": [{"livestock_type_id": str(sheep_type_id), "head_count": 4000}]},
    )

    rows = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.action == "application.update",
                AuditLog.object_id == _uuid.UUID(app_id),
            )
            .order_by(AuditLog.id)
        )
    ).scalars()
    entries = list(rows)
    assert len(entries) == 2
    herd_before = entries[-1].old_value["items"]
    herd_after = entries[-1].new_value["items"]
    assert [line["head_count"] for line in herd_before] == [40]
    assert [line["head_count"] for line in herd_after] == [4000]
    assert entries[0].old_value["items"] == [], "the first PATCH started from an empty herd"


async def test_moving_a_draft_to_another_contour_clears_the_frozen_version(
    db: AsyncSession,
    applicant_client,
    draft_ready_for_submission: str,
    published_contour,
    contours_layer,
    leshoz,
    approval_doc,
) -> None:
    """**A refused submission leaves a frozen version behind, and a later PATCH
    must not let it outlive its contour** (final review).

    Ruling 19 is deliberate: step 4 freezes `contour_version_id` and
    `requested_area_ha` BEFORE the signature, and a submission refused after
    that keeps them as evidence of a genuine attempt. What must not survive is
    the pair naming a plot the draft no longer points at — `max_approve_area`
    (decision #29) is compared against `requested_area_ha`, and a permit reads
    `contour_version_id` straight.

    The refusal is driven with a bad ERI, the cheapest way to reach step 8 with
    steps 1-7 having really run.
    """
    from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt

    app_id = draft_ready_for_submission
    refused = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert refused.status_code == 422, refused.text

    row = await db.get(Application, uuid.UUID(app_id))
    assert row is not None
    await db.refresh(row)
    assert row.contour_version_id is not None, "step 4 froze the pair before the signature"
    assert row.requested_area_ha is not None

    elsewhere = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db, elsewhere.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    await db.flush()

    patched = await applicant_client.patch(
        f"/api/v1/applications/{app_id}", json={"contour_id": str(elsewhere.id)}
    )
    assert patched.status_code == 200, patched.text

    await db.refresh(row)
    assert row.contour_id == elsewhere.id
    assert row.contour_version_id is None, "a version of the OLD contour cannot survive the move"
    assert row.requested_area_ha is None
