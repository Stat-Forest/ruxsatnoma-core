"""Plan ruling 3: migration `0019` never gave `inspector` the `permits.
view_any` grant tz/03's role matrix describes ("Инспектор: К" on
"Разрешение") — migration `0026` closes that gap. This is a regression test
for `permits`' own read route, not a duplicate of `test_role_grants_from_
migration_0026` in `test_models.py` (that one is a DB-level check; this one
proves the HTTP route actually honours it end to end)."""

from app.modules.permits.models import Permit


async def test_inspector_can_read_a_permit_via_the_new_grant(
    inspector_client, permit: Permit
) -> None:
    r = await inspector_client.get(f"/api/v1/permits/{permit.id}")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == str(permit.id)


async def test_a_zone_scoped_inspector_still_cannot_read_another_org_s_permit(
    db, other_leshoz, permit: Permit
) -> None:
    """The grant answers "at all" — zone scoping is a SEPARATE question
    (lesson) and still applies on top of it."""
    from tests.modules.auth.test_sessions import make_user
    from tests.modules.inspections.conftest import _client_for_user, unique_pinfl

    zoned_inspector = await make_user(
        db, role_code="inspector", organization_id=other_leshoz.id, pinfl=unique_pinfl()
    )
    async with _client_for_user(db, zoned_inspector) as client:
        r = await client.get(f"/api/v1/permits/{permit.id}")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-002"
