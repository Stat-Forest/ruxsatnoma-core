"""Task 7's own HTTP surface: certificates a caller may list/bind/unbind, one
object's signature list, and oversight re-verification.

Not the shared `conftest.py` (pre-flight ruling P4: later tasks add their own
fixtures rather than growing that file) -- `client_a`/`client_b`/
`client_oversight`/`a_signature`/`bound_cert_of_a` are this file's own, the
same local-fixture pattern every earlier task in this module already used
(`test_sign.py`'s `a_user`, `test_ri05.py`'s `a_user`, ...).

Every identity below (pinfl, certificate serial/issuer, object_id) is
randomised per call -- the test database is shared and persistent across
runs (`.claude/lessons.md`), and a fixed literal would make a later run
collide with an earlier one's leftovers instead of exercising the path the
test means to check."""

import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.modules.auth.models import User, UserPermission
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.signatures import service
from app.modules.signatures.models import Signature
from app.modules.signatures.permissions import REVERIFY, VIEW_ANY
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"
DOC = b"the-permit-bytes"


def _pinfl() -> str:
    """A fresh, valid-shape (`^[0-9]{14}$`) pinfl per call -- `users.pinfl` is
    UNIQUE and this file's fixtures may run many times against the shared
    test database (mirrors `test_sign.py`'s own helper)."""
    return f"{secrets.randbelow(10**14):014d}"


def _pkcs7(
    document: bytes,
    pinfl: str | None,
    *,
    serial: str | None = None,
    issuer: str | None = None,
    signed_at: datetime | None = None,
) -> str:
    """A fresh, globally-unique certificate identity per call unless the
    caller overrides `serial`/`issuer` explicitly -- avoids needing a
    shared-identity cleanup fixture the way `test_sign.py`'s fixed "SER-1"
    needs one. `pinfl` is typed `str | None` to match `User.pinfl`'s own
    nullable column so a caller can pass a fixture's `.pinfl` straight
    through without a per-call-site narrowing assert (lesson: an unannotated
    test parameter hides this exact `str | None` mismatch -- this file DOES
    annotate its fixtures, so the narrowing has to live somewhere real)."""
    assert pinfl is not None
    return encode_mock_signature(
        document=document,
        pinfl=pinfl,
        serial=serial or f"SER-{uuid.uuid4().hex[:12]}",
        issuer=issuer or f"ISS-{uuid.uuid4().hex[:8]}",
        signed_at=signed_at,
    )


def _commit_before_requests(client: httpx.AsyncClient, db: AsyncSession) -> None:
    """A fixture writing through `db` after the client was built is only
    flushed, not committed -- invisible to the app's own (different)
    connection until committed (`tests/modules/gis/conftest.py`'s own helper
    of the same shape, replicated locally rather than imported across
    modules). Committing again right before every outgoing request picks up
    whatever else `db` was given in the meantime, regardless of fixture
    order."""

    async def _commit(request: httpx.Request) -> None:
        await db.commit()

    client.event_hooks["request"] = [*client.event_hooks.get("request", []), _commit]


@asynccontextmanager
async def _client_for(db: AsyncSession, user: User) -> AsyncIterator[httpx.AsyncClient]:
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_before_requests(client, db)
        yield client


@pytest.fixture
async def user_a(db: AsyncSession) -> User:
    return await make_user(db, pinfl=_pinfl())


@pytest.fixture
async def user_b(db: AsyncSession) -> User:
    return await make_user(db, pinfl=_pinfl())


@pytest.fixture
async def client_a(db: AsyncSession, user_a: User) -> AsyncIterator[httpx.AsyncClient]:
    async with _client_for(db, user_a) as client:
        yield client


@pytest.fixture
async def client_b(db: AsyncSession, user_b: User) -> AsyncIterator[httpx.AsyncClient]:
    async with _client_for(db, user_b) as client:
        yield client


@pytest.fixture
async def client_oversight(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """`central_admin` in production holds both codes (brief) -- `view_any`
    and `reverify` -- so this fixture grants both, even though most tests
    using it only exercise one."""
    user = await make_user(db, pinfl=_pinfl())
    db.add(UserPermission(user_id=user.id, permission_code=VIEW_ANY))
    db.add(UserPermission(user_id=user.id, permission_code=REVERIFY))
    await db.flush()
    async with _client_for(db, user) as client:
        yield client


@pytest.fixture
async def a_signature(db: AsyncSession, user_a: User) -> Signature:
    """One valid signature, signed by `user_a` with a freshly bound
    certificate -- the shared basis `bound_cert_of_a` derives from, so the
    two can never disagree about which certificate produced it."""
    row = await service.sign(
        db,
        object_type="permit",
        object_id=uuid.uuid4(),
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(DOC, user_a.pinfl),
        user=user_a,
    )
    await db.commit()
    return row


@pytest.fixture
async def bound_cert_of_a(a_signature: Signature) -> uuid.UUID:
    return a_signature.certificate_id


# ---------------------------------------------------------------------------
# GET/POST/DELETE /certificates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_user_sees_only_their_own_certificates(client_a, client_b, bound_cert_of_a):
    mine = await client_a.get(f"{API}/certificates")
    assert [c["id"] for c in mine.json()["items"]] == [str(bound_cert_of_a)]
    theirs = await client_b.get(f"{API}/certificates")
    assert theirs.json()["items"] == []


@pytest.mark.asyncio
async def test_deleting_a_certificate_archives_it_and_keeps_the_signature_readable(
    client_a, bound_cert_of_a, a_signature
):
    resp = await client_a.delete(f"{API}/certificates/{bound_cert_of_a}")
    assert resp.status_code == 204
    listed = await client_a.get(
        f"{API}/signatures?object_type=permit&object_id={a_signature.object_id}"
    )
    assert listed.json()["items"][0]["verification_status"] == "valid"


@pytest.mark.asyncio
async def test_deleting_a_certificate_removes_it_from_the_owners_own_list(
    client_a, bound_cert_of_a
):
    """The list route's own `unbound_at IS NULL` filter (brief) -- checked
    directly, since the two given tests above never re-list `/certificates`
    after deleting."""
    await client_a.delete(f"{API}/certificates/{bound_cert_of_a}")
    resp = await client_a.get(f"{API}/certificates")
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_unbinding_someone_elses_certificate_is_refused(client_b, bound_cert_of_a):
    resp = await client_b.delete(f"{API}/certificates/{bound_cert_of_a}")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "ERR-SIGN-001"
    assert resp.json()["error"]["details"]["reason"] == "certificate_owned_by_another_user"


@pytest.mark.asyncio
async def test_unbinding_an_unknown_certificate_is_404(client_a):
    resp = await client_a.delete(f"{API}/certificates/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_register_certificate_binds_it_ahead_of_time(client_a, user_a: User):
    """`POST /certificates` is never reached by any of the four given tests
    -- untested otherwise (lesson: a public-surface task's own end-to-end
    test can ship the surface untested)."""
    pkcs7 = _pkcs7(b"a-signed-challenge", user_a.pinfl)
    resp = await client_a.post(f"{API}/certificates", json={"pkcs7": pkcs7})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["pinfl_or_stir"] == user_a.pinfl
    assert body["status"] == "active"

    listed = await client_a.get(f"{API}/certificates")
    assert body["id"] in [c["id"] for c in listed.json()["items"]]


@pytest.mark.asyncio
async def test_register_certificate_refuses_an_unproven_pinfl(client_a):
    """Ruling 4's "ownership proven by PINFL/STIR", refused: a certificate
    naming someone else's PINFL cannot be registered to `user_a`, matching
    `bind_certificate`'s own rule but enforced directly (`bind_certificate`
    itself leaves it silently unbound, since it has no signature to hang the
    refusal on -- Task 7's own route does have one)."""
    pkcs7 = _pkcs7(b"a-signed-challenge", _pinfl())  # someone else's pinfl
    resp = await client_a.post(f"{API}/certificates", json={"pkcs7": pkcs7})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "ERR-SIGN-001"
    assert resp.json()["error"]["details"]["reason"] == "certificate_pinfl_mismatch"


# ---------------------------------------------------------------------------
# GET /signatures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_owner_without_view_any_cannot_read_signatures(client_b, a_signature):
    resp = await client_b.get(
        f"{API}/signatures?object_type=permit&object_id={a_signature.object_id}"
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"


@pytest.mark.asyncio
async def test_view_any_holder_can_read_signatures_they_did_not_sign(db: AsyncSession, a_signature):
    _, token, csrf = await signed_in_with(db, VIEW_ANY)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        resp = await client.get(
            f"{API}/signatures?object_type=permit&object_id={a_signature.object_id}"
        )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


@pytest.mark.asyncio
async def test_sys_admin_reads_anyones_signatures_without_an_explicit_grant(
    db: AsyncSession, a_signature
):
    """Decision #41 ruling 2's superuser bypass, checked on THIS read path
    too, not just on `require_permission` -- `_holds_view_any` is a
    same-shaped, separately-written check and must agree with it (lesson: a
    superuser bypass must be reflected in every path that reports/uses
    permissions)."""
    admin = await make_user(db, role_code="sys_admin")
    await db.commit()
    async with _client_for(db, admin) as client:
        resp = await client.get(
            f"{API}/signatures?object_type=permit&object_id={a_signature.object_id}"
        )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


@pytest.mark.asyncio
async def test_signature_list_is_ordered_by_signed_at_then_id(client_a, user_a: User, db):
    """Created out of order, with a genuine tie, so the response can only be
    right if the route sorts by `(signed_at, id)` -- without it, `items[0]`
    depends on Postgres' own row order (pre-flight ruling P6)."""
    obj_id = uuid.uuid4()
    base = datetime.now(UTC)

    async def _sign(purpose: str, offset: timedelta) -> Signature:
        return await service.sign(
            db,
            object_type="permit",
            object_id=obj_id,
            purpose=purpose,
            document=DOC,
            pkcs7=_pkcs7(DOC, user_a.pinfl, signed_at=base + offset),
            user=user_a,
        )

    third = await _sign("p3", timedelta(seconds=20))
    first = await _sign("p1", timedelta(seconds=0))
    tie_a = await _sign("p2a", timedelta(seconds=10))
    tie_b = await _sign("p2b", timedelta(seconds=10))
    await db.commit()

    resp = await client_a.get(f"{API}/signatures?object_type=permit&object_id={obj_id}")
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["items"]]
    tie_pair = sorted((str(tie_a.id), str(tie_b.id)))
    assert ids == [str(first.id), *tie_pair, str(third.id)]


@pytest.mark.asyncio
async def test_signature_list_is_paged(client_a, user_a: User, db: AsyncSession):
    obj_id = uuid.uuid4()
    for i in range(3):
        await service.sign(
            db,
            object_type="permit",
            object_id=obj_id,
            purpose=f"purpose-{i}",
            document=DOC,
            pkcs7=_pkcs7(DOC, user_a.pinfl),
            user=user_a,
        )
    await db.commit()

    page1 = await client_a.get(
        f"{API}/signatures?object_type=permit&object_id={obj_id}&page=1&page_size=2"
    )
    body1 = page1.json()
    assert body1["total"] == 3
    assert len(body1["items"]) == 2
    assert body1["page"] == 1
    assert body1["page_size"] == 2

    page2 = await client_a.get(
        f"{API}/signatures?object_type=permit&object_id={obj_id}&page=2&page_size=2"
    )
    assert len(page2.json()["items"]) == 1


# ---------------------------------------------------------------------------
# POST /signatures/{id}/reverify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reverify_writes_a_new_record_and_never_rewrites_the_original(
    client_oversight, a_signature, db
):
    before = a_signature.verification
    resp = await client_oversight.post(f"{API}/signatures/{a_signature.id}/reverify")
    assert resp.status_code == 200
    await db.refresh(a_signature)
    assert a_signature.verification == before  # the original is evidence, not a cache


@pytest.mark.asyncio
async def test_reverify_needs_its_permission(client_a, a_signature):
    resp = await client_a.post(f"{API}/signatures/{a_signature.id}/reverify")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reverify_response_is_the_new_row_attached_to_the_object(
    client_oversight, a_signature
):
    resp = await client_oversight.post(f"{API}/signatures/{a_signature.id}/reverify")
    body = resp.json()
    assert body["id"] != str(a_signature.id)
    assert body["object_type"] == a_signature.object_type
    assert body["object_id"] == str(a_signature.object_id)
    assert body["purpose"] == f"{a_signature.purpose}:reverify"
    assert body["verification_status"] == "valid"


@pytest.mark.asyncio
async def test_reverify_of_an_unknown_signature_is_404(client_oversight):
    resp = await client_oversight.post(f"{API}/signatures/{uuid.uuid4()}/reverify")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reverify_reflects_a_certificate_revoked_after_signing(
    client_oversight, db: AsyncSession
):
    """The core claim behind ruling 5: a reverify re-derives the
    certificate's CURRENT standing, not the standing recorded at signing
    time. The mock adapter's `certificate_status` is a pure function of the
    serial's prefix (module docstring), so mutating the STORED row to a
    "REVOKED-" identity is how a test makes a LATER lookup answer
    differently from the one taken when it signed, without a stateful mock."""
    user = await make_user(db, pinfl=_pinfl())
    row = await service.sign(
        db,
        object_type="permit",
        object_id=uuid.uuid4(),
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(DOC, user.pinfl),
        user=user,
    )
    assert row.verification_status == "valid"
    cert = await service.get_certificate(db, row.certificate_id)
    cert.serial_number = f"REVOKED-{uuid.uuid4().hex[:10]}"
    await db.commit()

    resp = await client_oversight.post(f"{API}/signatures/{row.id}/reverify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["verification_status"] == "invalid"
    assert body["verification"]["reason"] == "certificate_revoked"

    await db.refresh(row)
    assert row.verification_status == "valid"  # the original stays untouched


@pytest.mark.asyncio
async def test_a_second_reverify_of_an_unchanged_certificate_is_refused(
    client_oversight, db: AsyncSession
):
    """The one collision the `:reverify` suffix trick does not avoid by
    itself: two reverifications of the SAME signature while its certificate
    stays `active` both land on the identical valid `(object_type,
    object_id, purpose:reverify)` slot ruling 8's partial index covers."""
    user = await make_user(db, pinfl=_pinfl())
    row = await service.sign(
        db,
        object_type="permit",
        object_id=uuid.uuid4(),
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(DOC, user.pinfl),
        user=user,
    )
    await db.commit()

    first = await client_oversight.post(f"{API}/signatures/{row.id}/reverify")
    assert first.status_code == 200

    second = await client_oversight.post(f"{API}/signatures/{row.id}/reverify")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ERR-SIGN-004"


@pytest.mark.asyncio
async def test_reverify_does_not_satisfy_a_missing_purpose(client_oversight, db: AsyncSession):
    """Confirms, in code and not just by inspection, the brief's own warning:
    a reverify record must never let `is_complete`/`missing_purposes` (Task
    6's public surface) count a requirement as satisfied (lesson: a
    public-surface task's own end-to-end test can ship the surface
    untested -- exercised here IN-PROCESS, the same functions 3.11 will call
    directly, not only over HTTP)."""
    user = await make_user(db, pinfl=_pinfl())
    obj_id = uuid.uuid4()
    row = await service.sign(
        db,
        object_type="permit",
        object_id=obj_id,
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(DOC, user.pinfl),
        user=user,
    )
    await db.commit()

    before = await service.missing_purposes(db, object_type="permit", object_id=obj_id)
    assert "permit_head" not in before  # already satisfied by the ORIGINAL valid signature

    resp = await client_oversight.post(f"{API}/signatures/{row.id}/reverify")
    assert resp.status_code == 200

    after = await service.missing_purposes(db, object_type="permit", object_id=obj_id)
    assert after == before  # the reverify record changed nothing about completeness
