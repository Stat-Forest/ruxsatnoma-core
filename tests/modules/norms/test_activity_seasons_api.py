"""Ruling #177 (stage 9): the `activity_seasons` dictionary's own CRUD
surface (`app/modules/norms/activity_seasons_router.py`) — creation,
zone scoping ("the leshoz for itself, the central admin for anyone"), the
uniqueness conflict, and the PATCH explicit-null rules. Resolution order
(a contour's own norm overriding the dictionary) and the season/minimum-term
CHECKS live in `test_checks.py`; this file is the admin surface alone."""

import uuid

import pytest
from httpx import AsyncClient

from app.modules.admin.models import Organization

pytestmark = pytest.mark.asyncio


async def _create(
    client: AsyncClient,
    organization_id: uuid.UUID,
    activity_type_id: uuid.UUID,
    **over,
):
    payload = {
        "organization_id": str(organization_id),
        "activity_type_id": str(activity_type_id),
        "season": {"windows": [{"from": "04-01", "to": "10-31"}]},
        "min_term_days": 30,
    } | over
    return await client.post("/api/v1/activity-seasons", json=payload)


async def test_a_leshoz_creates_its_own_dictionary_row(
    activity_seasons_leshoz_client: AsyncClient,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> None:
    created = await _create(activity_seasons_leshoz_client, leshoz.id, grazing_activity_id)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["organization_id"] == str(leshoz.id)
    assert body["activity_type_id"] == str(grazing_activity_id)
    assert body["season"] == {"windows": [{"from": "04-01", "to": "10-31"}]}
    assert body["min_term_days"] == 30


async def test_a_leshoz_cannot_create_for_another_organization(
    activity_seasons_leshoz_client: AsyncClient,
    other_leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> None:
    """Ruling #177's zone scoping — `_assert_organization_zone`, the SAME
    idiom `_assert_norm_zone` already uses for `Norm`."""
    refused = await _create(activity_seasons_leshoz_client, other_leshoz.id, grazing_activity_id)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "ERR-ACL-002"


async def test_the_central_admin_creates_for_any_organization(
    activity_seasons_central_client: AsyncClient,
    other_leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> None:
    created = await _create(activity_seasons_central_client, other_leshoz.id, grazing_activity_id)
    assert created.status_code == 201, created.text


async def test_a_second_row_for_the_same_pair_is_refused(
    activity_seasons_leshoz_client: AsyncClient,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> None:
    first = await _create(activity_seasons_leshoz_client, leshoz.id, grazing_activity_id)
    assert first.status_code == 201, first.text
    second = await _create(activity_seasons_leshoz_client, leshoz.id, grazing_activity_id)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "ERR-NORM-005"
    assert second.json()["error"]["details"]["reason"] == "already_exists"


async def test_no_permission_is_refused(
    applicant_client: AsyncClient,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> None:
    refused = await _create(applicant_client, leshoz.id, grazing_activity_id)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "ERR-ACL-001"


async def test_get_and_list_round_trip(
    activity_seasons_leshoz_client: AsyncClient,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
    haymaking_activity_id: uuid.UUID,
) -> None:
    grazing = await _create(activity_seasons_leshoz_client, leshoz.id, grazing_activity_id)
    assert grazing.status_code == 201, grazing.text
    haymaking = await _create(activity_seasons_leshoz_client, leshoz.id, haymaking_activity_id)
    assert haymaking.status_code == 201, haymaking.text

    fetched = await activity_seasons_leshoz_client.get(
        f"/api/v1/activity-seasons/{grazing.json()['id']}"
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["activity_type_id"] == str(grazing_activity_id)

    listed = await activity_seasons_leshoz_client.get(
        f"/api/v1/activity-seasons?organization_id={leshoz.id}"
        f"&activity_type_id={haymaking_activity_id}"
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == haymaking.json()["id"]


async def test_getting_an_unknown_row_is_404(activity_seasons_leshoz_client: AsyncClient) -> None:
    missing = await activity_seasons_leshoz_client.get(f"/api/v1/activity-seasons/{uuid.uuid4()}")
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["code"] == "ERR-SYS-003"


async def test_patch_replaces_the_season_and_can_clear_min_term_days(
    activity_seasons_leshoz_client: AsyncClient,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> None:
    created = await _create(activity_seasons_leshoz_client, leshoz.id, grazing_activity_id)
    row_id = created.json()["id"]

    patched = await activity_seasons_leshoz_client.patch(
        f"/api/v1/activity-seasons/{row_id}",
        json={
            "season": {"windows": [{"from": "11-01", "to": "03-31"}]},
            "min_term_days": None,
        },
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["season"] == {"windows": [{"from": "11-01", "to": "03-31"}]}
    assert body["min_term_days"] is None


async def test_patch_refuses_an_explicit_null_season(
    activity_seasons_leshoz_client: AsyncClient,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> None:
    """`season` backs a NOT NULL column — clearing the windows sends
    `{"windows": []}`, a real value, never JSON `null` (same reasoning as
    `OrganizationPatch._reject_explicit_null_gis_enabled`)."""
    created = await _create(activity_seasons_leshoz_client, leshoz.id, grazing_activity_id)
    row_id = created.json()["id"]

    patched = await activity_seasons_leshoz_client.patch(
        f"/api/v1/activity-seasons/{row_id}", json={"season": None}
    )
    assert patched.status_code == 422, patched.text


async def test_patch_by_another_organizations_leshoz_is_refused(
    activity_seasons_leshoz_client: AsyncClient,
    activity_seasons_other_leshoz_client: AsyncClient,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> None:
    created = await _create(activity_seasons_leshoz_client, leshoz.id, grazing_activity_id)
    row_id = created.json()["id"]

    refused = await activity_seasons_other_leshoz_client.patch(
        f"/api/v1/activity-seasons/{row_id}", json={"min_term_days": 5}
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "ERR-ACL-002"


async def test_create_refuses_an_unknown_activity_type(
    activity_seasons_leshoz_client: AsyncClient, leshoz: Organization
) -> None:
    refused = await _create(activity_seasons_leshoz_client, leshoz.id, uuid.uuid4())
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["details"]["reason"] == "unknown_activity_type"
