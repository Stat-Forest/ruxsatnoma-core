"""POST /api/v1/files (multipart) + GET /api/v1/files/{id}: types, magic bytes,
size cap, access rules, audit."""

import io
import uuid
from urllib.parse import quote

import pytest
from fastapi import UploadFile
from sqlalchemy import select

from app.core import files
from app.core.errors import DomainError
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


# --- C1 (final review): non-latin-1 filenames must not 500 the download ------------


async def test_download_cyrillic_filename_roundtrip(db):
    """C1 (final review): Starlette encodes header values latin-1, so a raw
    Cyrillic byte in `Content-Disposition` used to raise UnicodeEncodeError -> 500
    on every download of a file uploaded with a name like «доверенность.pdf».
    Fixed per RFC 6266/5987: an ASCII fallback `filename=` plus the exact original
    name in `filename*=UTF-8''...`."""
    _, token, csrf = await signed_in_with(db)
    await db.commit()
    app = create_app()
    filename = "доверенность.pdf"
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await upload(client, PDF, filename=filename)
        assert r.status_code == 201, r.text
        file_id = r.json()["id"]
        r2 = await client.get(f"{API}/files/{file_id}")
    assert r2.status_code == 200, r2.text
    assert r2.content == PDF
    disposition = r2.headers["content-disposition"]
    assert disposition.startswith("attachment")
    # The base name is pure Cyrillic (no ASCII survives) — the ASCII fallback
    # collapses to "file", keeping the ASCII extension.
    assert 'filename="file.pdf"' in disposition
    assert f"filename*=UTF-8''{quote(filename, safe='')}" in disposition


def test_ascii_fallback_filename_pure_non_ascii_collapses_to_file():
    """Unit-ish case for the pure-non-ASCII fallback: no extension at all survives
    either, so there is nothing to append to "file"."""
    assert files._ascii_fallback_filename("доверенность") == "file"


def test_ascii_fallback_filename_keeps_a_surviving_extension():
    assert files._ascii_fallback_filename("доверенность.pdf") == "file.pdf"


def test_ascii_fallback_filename_untouched_for_plain_ascii():
    assert files._ascii_fallback_filename("report.pdf") == "report.pdf"


def test_sanitize_filename_strips_bare_carriage_return():
    """T3 (deferred minor, absorbed here): `_sanitize_filename`'s docstring already
    promised newline safety, but a bare `\\r` with no accompanying `\\n` survived
    untouched — still a header-injection seam on clients that treat lone CR as a
    line terminator."""
    assert files.sanitize_filename("evil\rInjected: header") == "evilInjected: header"


def test_content_disposition_defends_itself_against_an_unsanitized_name():
    """Final fix wave, B1. A `"` is printable ASCII, so `_ascii_fallback_filename`
    keeps it and it closes the `filename="…"` value early: this helper used to emit
    `filename="a"b.pdf"` for `'a"b.pdf'`. Every caller happened to sanitize at
    ingest, so it was malformation rather than header injection — but the helper is
    now shared by three routers and stage 4 adds more, and a precondition stated in
    a docstring is enforced by nobody. It sanitizes its own input.

    Both halves are checked: exactly two `"` in the whole header (so the value is
    one token), and the extended parameter still carrying the sanitized name — a
    newline becomes a space, which percent-encodes to `%20`."""
    header = files.content_disposition("attachment", 'a"b\r\n.pdf')
    assert header.count('"') == 2, header
    assert 'filename="ab.pdf"' in header
    assert "\r" not in header and "\n" not in header
    assert "filename*=UTF-8''ab%20.pdf" in header


# --- I2 (final review): the size cap must be enforced before the body sits in RAM --


async def test_read_capped_content_length_fast_path_skips_reading():
    """A Content-Length already over the cap must reject before a single byte is
    read off the upload — proven with a stream that raises if `.read()` is ever
    invoked, so this fails loudly if the fast path regresses into always reading."""

    class _ExplodingStream(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise AssertionError("must not read once Content-Length already exceeds cap")

    file = UploadFile(_ExplodingStream(b""), filename="big.pdf")
    with pytest.raises(DomainError) as exc_info:
        await files.read_capped(file, cap_bytes=10, content_length=999)
    assert exc_info.value.code == "ERR-VAL-001"
    assert exc_info.value.details == {"reason": "too_large"}


async def test_read_capped_chunked_path_aborts_once_over_cap():
    """No Content-Length/`.size` known upfront: the body streams in chunks and
    aborts the instant the running total exceeds the cap. Tiny sizes only — this
    must never allocate anything close to a real oversized upload."""
    file = UploadFile(io.BytesIO(b"x" * 25), filename="small.pdf")
    with pytest.raises(DomainError) as exc_info:
        await files.read_capped(file, cap_bytes=20, content_length=None)
    assert exc_info.value.details == {"reason": "too_large"}


async def test_read_capped_returns_full_bytes_under_cap():
    file = UploadFile(io.BytesIO(PDF), filename="doc.pdf")
    data = await files.read_capped(file, cap_bytes=1024, content_length=len(PDF))
    assert data == PDF
