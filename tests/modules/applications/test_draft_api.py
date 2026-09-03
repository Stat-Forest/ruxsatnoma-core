"""The draft's four routes: create, patch, list, card (plan 03.9a task 3).

Every test here drives real HTTP through the fixtures in this package's
`conftest.py` — this is the module's first HTTP surface, so the three plumbing
pieces an HTTP-driven package needs (`_app_on_test_db`, the commit hook, the
re-exported gis fixtures) live there rather than in any one file.
"""

from datetime import date


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
    applicant_client,
    published_contour,
    grazing_activity_id,
    hodim_client,
    other_zone_hodim_client,
) -> None:
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]
    await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(grazing_activity_id),
        },
    )

    assert (await hodim_client.get(f"/api/v1/applications/{app_id}")).status_code == 200
    assert (await other_zone_hodim_client.get(f"/api/v1/applications/{app_id}")).status_code == 404


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
    applicant_client, published_contour, grazing_activity_id, hodim_client, other_zone_hodim_client
) -> None:
    """`assigned_org_id` is null until `start-review` writes the assignment
    (ruling 14), so the zone rule has to reach the CONTOUR's owner — otherwise a
    hodim's work queue is empty of exactly the applications they are supposed to
    pick up. The card next door resolves that per row; this proves the paged
    query resolves it the same way."""
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]
    await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(grazing_activity_id),
        },
    )

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
