"""POST/PATCH/archive /api/v1/admin/organizations: permissions, hierarchy, audit."""

import uuid

from sqlalchemy import func, select

from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.admin.permissions import ORGANIZATIONS_MANAGE
from app.modules.audit.models import AuditLog
from app.modules.auth.models import UserPermission
from tests.conftest import make_client
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"


async def signed_in_with(db, *codes: str):
    """A staff user holding `codes` as personal grants (role grants land in 3.3b)."""
    user = await make_user(db, role_code="executor_staff")
    for code in codes:
        db.add(UserPermission(user_id=user.id, permission_code=code))
    _, token, csrf = await make_session(db, user)
    await db.flush()
    return user, token, csrf


def auth_client(client, token: str, csrf: str):
    client.cookies.set("session", token)
    client.cookies.set("csrf_token", csrf)
    client.headers["X-CSRF-Token"] = csrf
    return client


async def test_get_organization_requires_permission(db, agency):
    _, token, csrf = await signed_in_with(db)  # no grants
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/organizations/{agency.id}")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_get_organization_returns_the_admin_shape_with_requisites(db, agency):
    """Finding 6 (whole-branch review): the ruling that dropped `requisites` from the
    public /refs schema assumed an admin read route existed — it didn't. Without this,
    `requisites` could only ever be seen in the response to a write."""
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    org = Organization(
        kind="leshoz",
        code=f"getone-{suffix}",
        name={"uz_cyrl": "Х", "uz_latn": "X"},
        parent_id=agency.id,
        stir="200388105",
        requisites={"account": "40012186035209704220"},
    )
    db.add(org)
    await db.flush()
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/organizations/{org.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == f"getone-{suffix}"
    assert body["requisites"] == {"account": "40012186035209704220"}


async def test_get_unknown_organization_is_404(db):
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/organizations/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"


async def test_create_requires_permission(db):
    _, token, csrf = await signed_in_with(db)  # no grants
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/organizations",
            json={
                "kind": "agency",
                "code": "a-1",
                "name": {"uz_cyrl": "Агентлик", "uz_latn": "Agentlik"},
            },
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_create_requires_csrf_header(db):
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        client.cookies.set("csrf_token", csrf)
        r = await client.post(
            f"{API}/admin/organizations",
            json={
                "kind": "agency",
                "code": "a-2",
                "name": {"uz_cyrl": "Агентлик", "uz_latn": "Agentlik"},
            },
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-AUTH-006"


async def test_create_leshoz_under_the_agency(db, agency):
    suffix = uuid.uuid4().hex[:6]
    user, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        leshoz = await client.post(
            f"{API}/admin/organizations",
            json={
                "kind": "leshoz",
                "parent_id": str(agency.id),
                "code": f"leshoz-{suffix}",
                "name": {"uz_cyrl": "Нукус ДЎХ", "uz_latn": "Nukus DOʻX"},
                "stir": "200388105",
                "requisites": {"account": "40012186035209704220"},
            },
        )
    assert leshoz.status_code == 201, leshoz.text
    assert leshoz.json()["stir"] == "200388105"
    assert leshoz.json()["status"] == "active"
    assert leshoz.json()["parent_id"] == str(agency.id)

    entries = (
        await db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.user_id == user.id, AuditLog.action == "organization.create")
        )
    ).scalar()
    assert entries == 1


async def test_second_agency_is_rejected_with_a_domain_error(db, agency):
    """The DB has a partial unique index; the service must catch this first so the
    admin sees ERR-VAL-001, not a 500 (ruling 6)."""
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/organizations",
            json={
                "kind": "agency",
                "code": "second-agency",
                "name": {"uz_cyrl": "Х", "uz_latn": "X"},
            },
        )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "root already exists"


async def test_wrong_parent_kind_rejected(db, agency):
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        # bolak may only hang off aylanma (ruling 6)
        r = await client.post(
            f"{API}/admin/organizations",
            json={
                "kind": "bolak",
                "parent_id": str(agency.id),
                "code": f"bolak-{suffix}",
                "name": {"uz_cyrl": "Бўлак", "uz_latn": "Boʻlak"},
            },
        )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ERR-VAL-001"
    assert r.json()["error"]["details"]["parent_kind"] == "agency"


async def test_non_agency_without_parent_rejected(db):
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/organizations",
            json={
                "kind": "leshoz",
                "code": "rootless-leshoz",
                "name": {"uz_cyrl": "ДЎХ", "uz_latn": "DOʻX"},
            },
        )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "parent required"


async def test_duplicate_code_rejected(db, agency):
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    db.add(
        Organization(
            kind="leshoz",
            code=f"dup-{suffix}",
            name={"uz_cyrl": "Х", "uz_latn": "X"},
            parent_id=agency.id,
        )
    )
    await db.flush()
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/organizations",
            json={
                "kind": "leshoz",
                "parent_id": str(agency.id),
                "code": f"dup-{suffix}",
                "name": {"uz_cyrl": "Х", "uz_latn": "X"},
            },
        )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["code"] == f"dup-{suffix}"


async def test_reparent_into_own_subtree_rejected(db, agency):
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    territorial = Organization(
        kind="territorial",
        code=f"t-{suffix}",
        name={"uz_cyrl": "Х", "uz_latn": "X"},
        parent_id=agency.id,
    )
    db.add(territorial)
    await db.flush()
    leshoz = Organization(
        kind="leshoz",
        code=f"l-{suffix}",
        name={"uz_cyrl": "Х", "uz_latn": "X"},
        parent_id=territorial.id,
    )
    db.add(leshoz)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.patch(
            f"{API}/admin/organizations/{territorial.id}",
            json={"parent_id": str(leshoz.id)},
        )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "cycle"


async def test_patch_updates_and_audits_old_value(db, agency):
    suffix = uuid.uuid4().hex[:6]
    user, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    org = Organization(
        kind="leshoz",
        code=f"p-{suffix}",
        name={"uz_cyrl": "Эски ном", "uz_latn": "Eski nom"},
        parent_id=agency.id,
    )
    db.add(org)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.patch(
            f"{API}/admin/organizations/{org.id}",
            json={
                "name": {"uz_cyrl": "Янги ном", "uz_latn": "Yangi nom"},
                "requisites": {"mfo": "00014"},
            },
        )
    assert r.status_code == 200
    assert r.json()["name"]["uz_cyrl"] == "Янги ном"
    assert r.json()["requisites"] == {"mfo": "00014"}

    entry = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.action == "organization.update", AuditLog.object_id == org.id)
            .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert entry.old_value["name"]["uz_cyrl"] == "Эски ном"
    assert entry.new_value["name"]["uz_cyrl"] == "Янги ном"


async def test_archive_blocked_while_active_children_exist(db, agency):
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    territorial = Organization(
        kind="territorial",
        code=f"ar-{suffix}",
        name={"uz_cyrl": "Х", "uz_latn": "X"},
        parent_id=agency.id,
    )
    db.add(territorial)
    await db.flush()
    child = Organization(
        kind="leshoz",
        code=f"ar-child-{suffix}",
        name={"uz_cyrl": "Х", "uz_latn": "X"},
        parent_id=territorial.id,
    )
    db.add(child)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        blocked = await client.post(f"{API}/admin/organizations/{territorial.id}/archive")
        assert blocked.status_code == 422
        assert blocked.json()["error"]["details"]["reason"] == "active children"

        await client.post(f"{API}/admin/organizations/{child.id}/archive")
        allowed = await client.post(f"{API}/admin/organizations/{territorial.id}/archive")
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "archived"


async def test_create_rejects_malformed_stir_with_422_not_500(db, agency):
    """The DB CHECK `stir_format` must never be the thing that catches this — a bad
    value should 422 at the schema boundary, not surface as ERR-SYS-001 (finding 4)."""
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/organizations",
            json={
                "kind": "leshoz",
                "parent_id": str(agency.id),
                "code": f"badstir-{suffix}",
                "name": {"uz_cyrl": "Х", "uz_latn": "X"},
                "stir": "12345",
            },
        )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "ERR-VAL-001"


async def test_patch_rejects_malformed_stir_with_422_not_500(db, agency):
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    org = Organization(
        kind="leshoz",
        code=f"patchstir-{suffix}",
        name={"uz_cyrl": "Х", "uz_latn": "X"},
        parent_id=agency.id,
    )
    db.add(org)
    await db.flush()
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.patch(
            f"{API}/admin/organizations/{org.id}", json={"stir": "not-nine-digits"}
        )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "ERR-VAL-001"


async def test_create_rejects_non_ascii_digit_stir_with_422_not_500(db, agency):
    """Re-review finding: `\\d` is Unicode-aware in both Pydantic and Python `re`, so
    a string of 9 Arabic-Indic digits satisfies `^\\d{9}$` even though it is not
    `^[0-9]{9}$` — and the Postgres CHECK (`stir ~ '^\\d{9}$'` is itself PCRE-ish but
    Postgres's `\\d` is ASCII-only) rejects it, reopening the exact 500 path finding 4
    closed. The pattern must use `[0-9]` explicitly."""
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/organizations",
            json={
                "kind": "leshoz",
                "parent_id": str(agency.id),
                "code": f"arabicstir-{suffix}",
                "name": {"uz_cyrl": "Х", "uz_latn": "X"},
                "stir": "١٢٣٤٥٦٧٨٩",  # nine Arabic-Indic digits, not ASCII 0-9
            },
        )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "ERR-VAL-001"


async def test_unknown_organization_is_404(db):
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.patch(
            f"{API}/admin/organizations/{uuid.uuid4()}",
            json={"name": {"uz_cyrl": "Х", "uz_latn": "X"}},
        )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"
