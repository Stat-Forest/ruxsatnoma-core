"""`saved_filters` CRUD (`/search/profiles/*`)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.search.permissions import SEARCH_USE
from tests.modules.search.conftest import _client_for


async def test_create_and_get_own_profile(db: AsyncSession):
    async for client in _client_for(db, SEARCH_USE):
        resp = await client.post(
            "/api/v1/search/profiles",
            json={
                "name": "My grazing filter",
                "kind": "applications",
                "params": {"status": "IN_REVIEW"},
            },
        )
        assert resp.status_code == 201, resp.text
        profile_id = resp.json()["id"]

        got = await client.get(f"/api/v1/search/profiles/{profile_id}")
        assert got.status_code == 200
        assert got.json()["params"] == {"status": "IN_REVIEW"}


async def test_duplicate_name_for_same_user_is_refused(db: AsyncSession):
    async for client in _client_for(db, SEARCH_USE):
        payload = {"name": "Dup", "kind": "permits", "params": {}}
        first = await client.post("/api/v1/search/profiles", json=payload)
        assert first.status_code == 201
        second = await client.post("/api/v1/search/profiles", json=payload)
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "ERR-SRCH-001"


async def test_a_stranger_cannot_see_or_edit_a_private_profile(db: AsyncSession):
    profile_id: str | None = None
    async for owner in _client_for(db, SEARCH_USE):
        created = await owner.post(
            "/api/v1/search/profiles",
            json={"name": "Private", "kind": "applications", "params": {}},
        )
        profile_id = created.json()["id"]
    assert profile_id is not None

    async for stranger in _client_for(db, SEARCH_USE):
        got = await stranger.get(f"/api/v1/search/profiles/{profile_id}")
        assert got.status_code == 404

        patched = await stranger.patch(
            f"/api/v1/search/profiles/{profile_id}", json={"name": "Hijacked"}
        )
        assert patched.status_code in (403, 404)


async def test_a_profile_shared_by_role_is_visible_to_that_role(db: AsyncSession):
    profile_id: str | None = None
    async for owner in _client_for(db, SEARCH_USE):
        created = await owner.post(
            "/api/v1/search/profiles",
            json={
                "name": "Shared with staff",
                "kind": "applications",
                "params": {},
                "shared": {"role_codes": ["executor_staff"], "user_ids": []},
            },
        )
        profile_id = created.json()["id"]
    assert profile_id is not None

    async for viewer in _client_for(db, SEARCH_USE):  # also executor_staff by default
        listed = await viewer.get("/api/v1/search/profiles")
        assert listed.status_code == 200
        ids = {row["id"] for row in listed.json()}
        assert profile_id in ids


async def test_owner_can_delete_own_profile(db: AsyncSession):
    async for client in _client_for(db, SEARCH_USE):
        created = await client.post(
            "/api/v1/search/profiles",
            json={"name": "To delete", "kind": "applications", "params": {}},
        )
        profile_id = created.json()["id"]
        deleted = await client.delete(f"/api/v1/search/profiles/{profile_id}")
        assert deleted.status_code == 204
        got = await client.get(f"/api/v1/search/profiles/{profile_id}")
        assert got.status_code == 404
