"""The tariff API. Same machinery as parameters, plus the activity/group key and
the `on_date` lookup a calculation and a front-end both use."""

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_the_rate_in_force_is_returned_for_a_date(applicant_client: AsyncClient) -> None:
    """The seeded VMQ 278 rates are in force from 2015-09-30 with no end."""
    response = await applicant_client.get(
        "/api/v1/tariffs?activity_code=haymaking&on_date=2026-08-30"
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["coefficient"] == "1.500000"
    assert items[0]["quantity_unit"] == "ha"


async def test_grazing_returns_all_four_groups(applicant_client: AsyncClient) -> None:
    response = await applicant_client.get(
        "/api/v1/tariffs?activity_code=grazing&on_date=2026-08-30"
    )
    groups = {item["livestock_group"]: item["coefficient"] for item in response.json()["items"]}
    assert groups == {
        "large_adult": "0.450000",
        "large_young": "0.150000",
        "small_adult": "0.100000",
        "small_young": "0.030000",
    }


async def test_a_tariff_for_a_date_before_it_took_effect_is_not_returned(
    applicant_client: AsyncClient,
) -> None:
    response = await applicant_client.get(
        "/api/v1/tariffs?activity_code=haymaking&on_date=2015-01-01"
    )
    assert response.json()["items"] == []


async def test_creating_a_tariff_needs_the_manage_permission(
    gis_specialist_client: AsyncClient, grazing_activity_id: uuid.UUID
) -> None:
    """A leshoz GIS specialist may draft a NORM but not a republic-wide tariff
    (decision #32): tariffs are set centrally."""
    response = await gis_specialist_client.post(
        "/api/v1/tariffs",
        json={
            "activity_type_id": str(grazing_activity_id),
            "livestock_group": "large_adult",
            "coefficient": "0.5",
            "quantity_unit": "head",
            "effective_from": "2030-01-01",
            "basis": "t",
        },
    )
    assert response.status_code == 403


async def test_creating_a_tariff_with_an_unknown_activity_type_is_refused(
    tariffs_maker_client: AsyncClient,
) -> None:
    """`create_versioned`'s own guard (fix round 1): a garbage
    `activity_type_id` must not reach `flush()` and surface as an uncaught
    `IntegrityError` -> `ERR-SYS-001`/500."""
    response = await tariffs_maker_client.post(
        "/api/v1/tariffs",
        json={
            "activity_type_id": str(uuid.uuid4()),
            "coefficient": "1.0",
            "quantity_unit": "ha",
            "effective_from": "2034-01-01",
            "basis": "t",
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["reason"] == "unknown_activity_type"


async def test_a_benefit_modifier_round_trips(
    tariffs_maker_client: AsyncClient, haymaking_activity_id: uuid.UUID, benefit_category: str
) -> None:
    """The code must resolve against the `benefit_categories` classifier (I7),
    which is why this now needs a real category rather than a bare string."""
    response = await tariffs_maker_client.post(
        "/api/v1/tariffs",
        json={
            "activity_type_id": str(haymaking_activity_id),
            "coefficient": "1.5",
            "quantity_unit": "ha",
            "benefit_modifiers": {benefit_category: "0.5"},
            "effective_from": "2030-01-01",
            "basis": "t",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["benefit_modifiers"] == {benefit_category: "0.5"}


async def test_an_unknown_benefit_category_is_refused(
    tariffs_maker_client: AsyncClient, haymaking_activity_id: uuid.UUID
) -> None:
    """I7: ruling 20 defines `benefit_modifiers`' keys as `benefit_categories`
    classifier item codes, but nothing checked them — a typo created a benefit
    nobody can claim, or one nobody intended. Same guard as
    `unknown_activity_type`, one field over."""
    response = await tariffs_maker_client.post(
        "/api/v1/tariffs",
        json={
            "activity_type_id": str(haymaking_activity_id),
            "coefficient": "1.5",
            "quantity_unit": "ha",
            "benefit_modifiers": {"not_a_real_category": "0.5"},
            "effective_from": "2030-02-01",
            "basis": "t",
        },
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]["details"]
    assert error["reason"] == "unknown_benefit_category"
    assert error["codes"] == ["not_a_real_category"]


async def test_patching_in_an_unknown_benefit_category_is_refused(
    tariffs_maker_client: AsyncClient, haymaking_activity_id: uuid.UUID
) -> None:
    """The same guard on the other verb: validating only `POST` would leave
    the hole open one HTTP method over."""
    created = await tariffs_maker_client.post(
        "/api/v1/tariffs",
        json={
            "activity_type_id": str(haymaking_activity_id),
            "coefficient": "1.5",
            "quantity_unit": "ha",
            "effective_from": "2030-03-01",
            "basis": "t",
        },
    )
    patched = await tariffs_maker_client.patch(
        f"/api/v1/tariffs/{created.json()['id']}",
        json={"benefit_modifiers": {"not_a_real_category": "0.5"}},
    )
    assert patched.status_code == 422, patched.text
    assert patched.json()["error"]["details"]["reason"] == "unknown_benefit_category"


@pytest.mark.parametrize("modifier", ["abc", "-1", "1.5", "NaN", "Infinity", "-Infinity"])
async def test_a_benefit_modifier_outside_zero_to_one_is_refused(
    tariffs_maker_client: AsyncClient,
    haymaking_activity_id: uuid.UUID,
    benefit_category: str,
    modifier: str,
) -> None:
    """I7: the values reached `calculator._apply_benefit` as
    `coefficient * Decimal(modifier)` with no guard. `"abc"` was an uncaught
    `InvalidOperation` — a 500 — and `"-1"` produced a negative coefficient,
    hence a negative `amount` that `preview` returned happily and
    `save_calculation` turned into an `amount >= 0` CHECK violation, another
    500. Above 1 is refused too: a multiplier that RAISES the fee is not a
    benefit.

    `"NaN"` is the same defect class as `"abc"`, but slipped past the first
    fix (re-review of the fix wave): `Decimal("NaN")` parses without error,
    and the bound comparison then raises `InvalidOperation` — an
    `ArithmeticError`, not a `ValueError` — which pydantic does not turn into
    a 422. `"Infinity"`/`"-Infinity"` parse AND compare cleanly (ordering
    against infinity never raises), so they were already refused correctly
    through the normal out-of-bounds `ValueError`; kept here to pin that they
    stay that way."""
    response = await tariffs_maker_client.post(
        "/api/v1/tariffs",
        json={
            "activity_type_id": str(haymaking_activity_id),
            "coefficient": "1.5",
            "quantity_unit": "ha",
            "benefit_modifiers": {benefit_category: modifier},
            "effective_from": "2030-04-01",
            "basis": "t",
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ERR-VAL-001"


@pytest.mark.parametrize(
    ("field", "value"),
    [("livestock_group", "large_adults"), ("quantity_unit", "furlong")],
)
async def test_an_out_of_range_enum_is_a_422_not_a_500(
    tariffs_maker_client: AsyncClient, grazing_activity_id: uuid.UUID, field: str, value: str
) -> None:
    """I8: neither field had a schema bound. `livestock_group` had a DB CHECK,
    so a typo flushed into an `IntegrityError` `main.py` does not handle — a
    500, not a 422 — and `quantity_unit` had no bound on EITHER side, so any
    string was storable and was copied verbatim into an immutable
    calculation's `breakdown`. Both are `Literal`s now, built from the same
    tuples the DB CHECKs are (migration 0013 adds the missing one)."""
    payload = {
        "activity_type_id": str(grazing_activity_id),
        "coefficient": "0.5",
        "quantity_unit": "head",
        "effective_from": "2035-01-01",
        "basis": "t",
    } | {field: value}
    response = await tariffs_maker_client.post("/api/v1/tariffs", json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ERR-VAL-001"


async def test_patching_a_draft_tariff_updates_only_the_given_fields(
    tariffs_maker_client: AsyncClient, haymaking_activity_id: uuid.UUID
) -> None:
    """Regression test: `update_versioned` mutates the row then flushes, and
    `updated_at` (`onupdate=func.now()`) comes back EXPIRED from a plain UPDATE
    (unlike an INSERT, which gets it via RETURNING) — reading it in the audit
    snapshot without a `db.refresh()` first raised `MissingGreenlet`, a 500 on
    every successful PATCH. None of the brief's own PATCH tests reach this path
    (they all hit the `not_draft` refusal first)."""
    created = await tariffs_maker_client.post(
        "/api/v1/tariffs",
        json={
            "activity_type_id": str(haymaking_activity_id),
            "coefficient": "2.0",
            "quantity_unit": "ha",
            "effective_from": "2031-01-01",
            "basis": "t",
        },
    )
    tariff_id = created.json()["id"]

    patched = await tariffs_maker_client.patch(
        f"/api/v1/tariffs/{tariff_id}", json={"coefficient": "3.0"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["coefficient"] == "3.000000"
    assert patched.json()["quantity_unit"] == "ha"  # untouched field survives


async def test_a_maker_archives_their_own_draft_and_it_is_idempotent(
    tariffs_maker_client: AsyncClient, haymaking_activity_id: uuid.UUID
) -> None:
    """Same `MissingGreenlet` regression as the PATCH test above —
    `archive_versioned` flushes then snapshots the row for the audit trail
    too. Archiving a DRAFT needs no second person — maker-checker binds a row
    already IN FORCE, not the maker's own not-yet-published draft — and
    archiving twice is a no-op (mirrors `admin.service.
    archive_classifier_item`). An archived row is no longer a draft either."""
    created = await tariffs_maker_client.post(
        "/api/v1/tariffs",
        json={
            "activity_type_id": str(haymaking_activity_id),
            "coefficient": "2.0",
            "quantity_unit": "ha",
            "effective_from": "2031-06-01",
            "basis": "t",
        },
    )
    tariff_id = created.json()["id"]

    archived = await tariffs_maker_client.post(f"/api/v1/tariffs/{tariff_id}/archive")
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"
    assert archived.json()["effective_to"] is not None

    archived_again = await tariffs_maker_client.post(f"/api/v1/tariffs/{tariff_id}/archive")
    assert archived_again.status_code == 200, archived_again.text

    blocked = await tariffs_maker_client.patch(
        f"/api/v1/tariffs/{tariff_id}", json={"coefficient": "9.0"}
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["details"]["reason"] == "not_draft"


async def test_a_checker_archives_a_published_tariff(
    tariffs_maker_client: AsyncClient,
    tariffs_checker_client: AsyncClient,
    science_activity_id: uuid.UUID,
) -> None:
    """Regression test for the archive permission fix: taking a row already IN
    FORCE out of force is the one-person change maker-checker exists to
    prevent, so `archive_versioned` requires `TARIFFS_PUBLISH` for a
    `published` row specifically (checked in the service, not only at the
    route — the route accepts either permission so a maker still reaches a
    domain answer, per `refs_router.py`'s module docstring). `science` has no
    seeded tariff (`test_science_has_no_tariff`), so publishing a fresh one
    here cannot trip `ex_tariffs_one_in_force` against the seed data."""
    created = await tariffs_maker_client.post(
        "/api/v1/tariffs",
        json={
            "activity_type_id": str(science_activity_id),
            "coefficient": "2.0",
            "quantity_unit": "ha",
            "effective_from": "2031-01-01",
            "basis": "t",
        },
    )
    tariff_id = created.json()["id"]
    published = await tariffs_checker_client.post(f"/api/v1/tariffs/{tariff_id}/publish")
    assert published.status_code == 200, published.text

    archived = await tariffs_checker_client.post(f"/api/v1/tariffs/{tariff_id}/archive")
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"
    assert archived.json()["effective_to"] is not None


async def test_a_maker_cannot_archive_a_published_tariff(
    tariffs_maker_client: AsyncClient,
    tariffs_checker_client: AsyncClient,
    science_activity_id: uuid.UUID,
) -> None:
    """The invariant the archive permission check exists to defend: a maker
    (`TARIFFS_MANAGE` only) holds enough permission to REACH `/archive` at all
    (so they can still discard their own draft, the test above), but not to
    single-handedly take a row someone else already published out of force."""
    created = await tariffs_maker_client.post(
        "/api/v1/tariffs",
        json={
            "activity_type_id": str(science_activity_id),
            "coefficient": "2.0",
            "quantity_unit": "ha",
            "effective_from": "2032-01-01",
            "basis": "t",
        },
    )
    tariff_id = created.json()["id"]
    published = await tariffs_checker_client.post(f"/api/v1/tariffs/{tariff_id}/publish")
    assert published.status_code == 200, published.text

    blocked = await tariffs_maker_client.post(f"/api/v1/tariffs/{tariff_id}/archive")
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["error"]["code"] == "ERR-ACL-001"


async def test_the_create_response_shows_the_stored_precision_not_the_caller_s(
    tariffs_maker_client: AsyncClient, haymaking_activity_id: uuid.UUID
) -> None:
    """Regression test: `coefficient` is `numeric(12,6)`; posting `"1.5"` pads to
    `1.500000` in Postgres, but INSERT's implicit RETURNING only refreshes
    server-generated columns, not ones the caller supplied — without the
    `db.refresh()` in `create_versioned`, this response echoed the caller's own
    unpadded `"1.5"` while every other read of the same row already showed
    `"1.500000"` (same family of bug as the two regression tests above, same
    fixed-scale-NUMERIC lesson, a different column and a different service
    function than either)."""
    response = await tariffs_maker_client.post(
        "/api/v1/tariffs",
        json={
            "activity_type_id": str(haymaking_activity_id),
            "coefficient": "1.5",
            "quantity_unit": "ha",
            "effective_from": "2033-01-01",
            "basis": "t",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["coefficient"] == "1.500000"
