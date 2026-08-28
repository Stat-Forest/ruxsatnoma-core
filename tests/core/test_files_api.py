"""POST /api/v1/files (multipart) + GET /api/v1/files/{id}: types, magic bytes,
size cap, access rules, audit."""

import io
import uuid

from sqlalchemy import select

from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Applicant, User
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"
PDF = b"%PDF-1.7 fake body"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


def upload(client, content: bytes, *, filename="doc.pdf", content_type="application/pdf"):
    return client.post(
        f"{API}/files", files={"file": (filename, io.BytesIO(content), content_type)}
    )


def unique_pinfl() -> str:
    # Leading digit 7: 3/4/5/6 are already claimed by the other auth test modules
    # (oneid/eimzo/registration/legal_applicants) sharing this same persistent test DB.
    return f"7{uuid.uuid4().int % 10**13:013d}"


async def registered_applicant(db, **overrides) -> User:
    """An applicant-role user with a linked `Applicant` profile, so it passes the
    registration gate (ERR-AUTH-008 in get_current_user) — mirrors how
    tests/modules/auth/test_applicant_models.py's `individual()` helper builds one,
    since the HTTP OneID/complete-registration flow the auth suites otherwise use is
    unnecessary weight for a files-access test."""
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl(), **overrides)
    db.add(
        Applicant(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    )
    await db.flush()
    return user


async def test_upload_pdf_and_download_roundtrip(db):
    user, token, csrf = await signed_in_with(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await upload(client, PDF)
        assert r.status_code == 201, r.text
        meta = r.json()
        assert meta["content_type"] == "application/pdf"
        assert meta["size_bytes"] == len(PDF)
        assert len(meta["sha256"]) == 64
        r2 = await client.get(f"{API}/files/{meta['id']}")
        assert r2.status_code == 200
        assert r2.content == PDF
        assert r2.headers["content-disposition"].startswith("attachment")
        assert r2.headers["x-content-type-options"] == "nosniff"


async def test_upload_writes_audit(db):
    user, token, csrf = await signed_in_with(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await upload(client, PDF)
    row = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == "file.upload",
                AuditLog.object_id == uuid.UUID(r.json()["id"]),
            )
        )
    ).scalar_one()
    assert row.user_id == user.id


async def test_png_is_served_inline(db):
    _, token, csrf = await signed_in_with(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await upload(client, PNG, filename="pic.png", content_type="image/png")
        assert r.status_code == 201
        r2 = await client.get(f"{API}/files/{r.json()['id']}")
        assert r2.headers["content-disposition"].startswith("inline")


async def test_disallowed_type_rejected(db):
    _, token, csrf = await signed_in_with(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await upload(
            client, b"MZ...", filename="x.exe", content_type="application/x-msdownload"
        )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ERR-VAL-001"


async def test_magic_bytes_must_match_declared_type(db):
    _, token, csrf = await signed_in_with(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await upload(client, b"<html>not a pdf</html>")
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "content_mismatch"


async def test_size_cap_enforced(db):
    """max_upload_mb=20 default; a 21MB body is rejected before touching MinIO."""
    _, token, csrf = await signed_in_with(db)
    await db.commit()
    app = create_app()
    big = b"%PDF-" + b"\x00" * (21 * 1024 * 1024)
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await upload(client, big)
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "too_large"


async def test_upload_requires_auth(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await upload(client, PDF)
    assert r.status_code == 401


async def test_owner_reads_own_file_staff_reads_all_stranger_applicant_cannot(db):
    owner_role_user = await registered_applicant(db)
    _, owner_token, owner_csrf = await make_session(db, owner_role_user)
    staff, staff_token, staff_csrf = await signed_in_with(db)  # executor_staff, no grants
    stranger = await registered_applicant(db)
    _, str_token, str_csrf = await make_session(db, stranger)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, owner_token, owner_csrf)
        file_id = (await upload(client, PDF)).json()["id"]
        r_owner = await client.get(f"{API}/files/{file_id}")
        assert r_owner.status_code == 200
    async with make_client(app, lifespan=True) as client:
        auth_client(client, staff_token, staff_csrf)
        assert (await client.get(f"{API}/files/{file_id}")).status_code == 200
    async with make_client(app, lifespan=True) as client:
        auth_client(client, str_token, str_csrf)
        r = await client.get(f"{API}/files/{file_id}")
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_unknown_file_is_404(db):
    _, token, csrf = await signed_in_with(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/files/{uuid.uuid4()}")
    assert r.status_code == 404
