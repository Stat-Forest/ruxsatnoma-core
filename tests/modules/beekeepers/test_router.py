"""beekeepers HTTP API: permission gate, CRUD, remove, lookup — every route
walked for real (client -> router -> service -> repo -> DB), never a
service-level call standing in for the route."""

import uuid

from sqlalchemy import select

from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.auth import service as auth_service
from app.modules.auth.models import UserPermission
from app.modules.beekeepers.permissions import BEEKEEPERS_MANAGE
from app.modules.integrations.adapters.oneid import OneIdProfile
from tests.conftest import make_client
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"


def unique_pinfl() -> str:
    # Leading digit 4 (tests/modules/gis/conftest.py's own comment names the
    # digits already claimed by other packages on this shared, persistent
    # test DB: 1/2/3/5/6/7/9).
    return f"4{uuid.uuid4().int % 10**13:013d}"


async def registrar_client(db, *codes: str):
    """A staff user holding `codes` as personal grants — the same shape
    `tests/modules/admin/test_organizations_admin.py::signed_in_with` uses
    (role grants for `beekeepers.manage` land only through migration 0053's
    seeded `beekeeping_registrar` role; a personal grant on a plain staff
    user proves the PERMISSION gate on its own, independent of that seed)."""
    user = await make_user(db, role_code="executor_staff")
    for code in codes:
        db.add(UserPermission(user_id=user.id, permission_code=code))
    _, token, csrf = await make_session(db, user)
    await db.commit()
    return user, token, csrf


def auth_client(client, token: str, csrf: str):
    client.cookies.set("session", token)
    client.cookies.set("csrf_token", csrf)
    client.headers["X-CSRF-Token"] = csrf
    return client


async def test_list_requires_the_permission(db):
    _, token, csrf = await registrar_client(db)  # no grants
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/beekeepers")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_create_requires_the_permission(db):
    _, token, csrf = await registrar_client(db)  # no grants
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/beekeepers",
            json={
                "certificate_no": f"T-{uuid.uuid4().hex[:8]}",
                "pinfl": unique_pinfl(),
                "passport_series": "AB",
                "passport_number": "1234567",
                "full_name": "No Permission",
            },
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_lookup_requires_the_permission(db):
    _, token, csrf = await registrar_client(db)  # no grants
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/beekeepers/lookup", params={"pinfl": unique_pinfl()})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_create_then_list_patch_and_remove(db):
    _, token, csrf = await registrar_client(db, BEEKEEPERS_MANAGE)
    app = create_app()
    certificate_no = f"AUZ-{uuid.uuid4().hex[:8]}"
    pinfl = unique_pinfl()

    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)

        r_create = await client.post(
            f"{API}/beekeepers",
            json={
                "certificate_no": certificate_no,
                "pinfl": pinfl,
                "passport_series": "AB",
                "passport_number": "1234567",
                "full_name": "Test Beekeeper",
                "farm_name": "Test Farm",
            },
        )
        assert r_create.status_code == 201, r_create.text
        body = r_create.json()
        beekeeper_id = body["id"]
        assert body["status"] == "active"
        assert body["certificate_no"] == certificate_no
        assert body["removed_reason"] is None

        # A second row under the SAME certificate number is refused —
        # `uq_beekeepers_certificate_no_active`'s own domain-error pre-check.
        r_dup = await client.post(
            f"{API}/beekeepers",
            json={
                "certificate_no": certificate_no,
                "pinfl": unique_pinfl(),
                "passport_series": "AC",
                "passport_number": "7654321",
                "full_name": "Someone Else",
            },
        )
        assert r_dup.status_code == 422
        assert r_dup.json()["error"]["code"] == "ERR-VAL-001"

        # Found by `q` over certificate_no/full_name/pinfl.
        r_list = await client.get(f"{API}/beekeepers", params={"q": certificate_no})
        assert r_list.status_code == 200
        items = r_list.json()["items"]
        assert any(item["id"] == beekeeper_id for item in items)

        r_patch = await client.patch(
            f"{API}/beekeepers/{beekeeper_id}", json={"farm_name": "Renamed Farm"}
        )
        assert r_patch.status_code == 200
        assert r_patch.json()["farm_name"] == "Renamed Farm"
        assert r_patch.json()["certificate_no"] == certificate_no  # untouched field survives

        # `reason` is mandatory (min_length=1) — an empty one is a 422, never
        # a silent no-op remove.
        r_bad_remove = await client.post(
            f"{API}/beekeepers/{beekeeper_id}/remove", json={"reason": "   "}
        )
        assert r_bad_remove.status_code == 422
        assert r_bad_remove.json()["error"]["code"] == "ERR-VAL-001"

        r_remove = await client.post(
            f"{API}/beekeepers/{beekeeper_id}/remove", json={"reason": "left the union"}
        )
        assert r_remove.status_code == 200
        assert r_remove.json()["status"] == "removed"
        assert r_remove.json()["removed_reason"] == "left the union"

        # Removing an already-removed row is refused (never a DELETE, never a
        # silent second remove).
        r_remove_again = await client.post(
            f"{API}/beekeepers/{beekeeper_id}/remove", json={"reason": "again"}
        )
        assert r_remove_again.status_code == 422
        assert r_remove_again.json()["error"]["code"] == "ERR-VAL-001"

        # The certificate number is free again for a NEW active registration
        # — the whole point of the partial unique index.
        r_recreate = await client.post(
            f"{API}/beekeepers",
            json={
                "certificate_no": certificate_no,
                "pinfl": unique_pinfl(),
                "passport_series": "AD",
                "passport_number": "2222222",
                "full_name": "Re-registered Beekeeper",
            },
        )
        assert r_recreate.status_code == 201, r_recreate.text

    rows = (
        (
            await db.execute(
                select(AuditLog.action).where(AuditLog.object_id == uuid.UUID(beekeeper_id))
            )
        )
        .scalars()
        .all()
    )
    assert {"beekeeper.create", "beekeeper.update", "beekeeper.remove"} <= set(rows)


async def test_lookup_from_a_stored_oneid_profile(db):
    pinfl = unique_pinfl()
    await auth_service.login_or_create_by_pinfl(
        db,
        pinfl=pinfl,
        full_name="OneID Test User",
        method="oneid",
        snapshot=OneIdProfile(
            pinfl=pinfl, full_name="OneID Test User", passport="AB1234567"
        ).to_snapshot(),
        phone=None,
        ip=None,
        user_agent=None,
    )
    await db.commit()

    _, token, csrf = await registrar_client(db, BEEKEEPERS_MANAGE)
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/beekeepers/lookup", params={"pinfl": pinfl})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "full_name": "OneID Test User",
        "passport_series": "AB",
        "passport_number": "1234567",
    }


async def test_lookup_404_without_a_prior_oneid_login(db):
    _, token, csrf = await registrar_client(db, BEEKEEPERS_MANAGE)
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/beekeepers/lookup", params={"pinfl": unique_pinfl()})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"
