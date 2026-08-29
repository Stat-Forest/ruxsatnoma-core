"""OTP request/verify: delivery via the outbox (mock sender), attempts cap, rate
limit, token issue.

Targets are unique per test AND per run: the test DB is persistent and the rate
limit counts requests per target over the last hour — a fixed phone number would
start returning 429 after a few consecutive full-suite runs.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.core.security import hash_otp
from app.main import create_app
from app.modules.auth import repo
from app.modules.auth.models import OtpCode
from app.modules.auth.service import _mask_target
from app.modules.integrations import service as integrations_service
from app.modules.integrations.adapters.otp_sender import MockOtpSender, get_otp_sender
from tests.conftest import make_client
from tests.modules.auth.conftest import _delivered_code

API = "/api/v1"


def unique_phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


def unique_email() -> str:
    return f"otp-{uuid.uuid4().hex[:10]}@test.uz"


async def request_and_verify(client, db, *, target, target_type="phone", purpose="phone_verify"):
    r = await client.post(
        f"{API}/auth/otp/request",
        json={"target_type": target_type, "target": target, "purpose": purpose},
    )
    assert r.status_code == 204
    code = await _delivered_code(db)
    r = await client.post(
        f"{API}/auth/otp/verify", json={"target": target, "code": code, "purpose": purpose}
    )
    assert r.status_code == 200
    return r.json()["otp_token"]


async def test_full_phone_otp_flow(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        token = await request_and_verify(client, db, target=unique_phone())
    assert len(token) >= 43


async def test_wrong_code_then_correct(db):
    phone = unique_phone()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await client.post(
            f"{API}/auth/otp/request",
            json={"target_type": "phone", "target": phone, "purpose": "phone_verify"},
        )
        bad = await client.post(
            f"{API}/auth/otp/verify",
            json={"target": phone, "code": "000000", "purpose": "phone_verify"},
        )
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "ERR-AUTH-010"
        good = await client.post(
            f"{API}/auth/otp/verify",
            json={"target": phone, "code": await _delivered_code(db), "purpose": "phone_verify"},
        )
        assert good.status_code == 200


async def test_attempts_cap_burns_code(db):
    phone = unique_phone()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await client.post(
            f"{API}/auth/otp/request",
            json={"target_type": "phone", "target": phone, "purpose": "phone_verify"},
        )
        for _ in range(5):
            await client.post(
                f"{API}/auth/otp/verify",
                json={"target": phone, "code": "000000", "purpose": "phone_verify"},
            )
        r = await client.post(
            f"{API}/auth/otp/verify",
            json={"target": phone, "code": await _delivered_code(db), "purpose": "phone_verify"},
        )
    assert r.status_code == 400  # correct code no longer accepted


async def test_rate_limit_429(db):
    phone = unique_phone()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        for _ in range(5):
            r = await client.post(
                f"{API}/auth/otp/request",
                json={"target_type": "phone", "target": phone, "purpose": "phone_verify"},
            )
            assert r.status_code == 204
        r = await client.post(
            f"{API}/auth/otp/request",
            json={"target_type": "phone", "target": phone, "purpose": "phone_verify"},
        )
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "ERR-AUTH-009"


async def test_expired_code_rejected(db):
    phone = unique_phone()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await client.post(
            f"{API}/auth/otp/request",
            json={"target_type": "phone", "target": phone, "purpose": "phone_verify"},
        )
        await db.execute(
            update(OtpCode)
            .where(OtpCode.target == phone)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await db.commit()
        r = await client.post(
            f"{API}/auth/otp/verify",
            json={"target": phone, "code": await _delivered_code(db), "purpose": "phone_verify"},
        )
    assert r.status_code == 400


async def test_email_flow_and_validation(db):
    email = unique_email()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        bad = await client.post(
            f"{API}/auth/otp/request",
            json={"target_type": "email", "target": "not-an-email", "purpose": "email_verify"},
        )
        assert bad.status_code == 422
        mismatch = await client.post(
            f"{API}/auth/otp/request",
            json={"target_type": "email", "target": email, "purpose": "phone_verify"},
        )
        assert mismatch.status_code == 422
        token = await request_and_verify(
            client, db, target=email, target_type="email", purpose="email_verify"
        )
        assert token


async def test_mfa_purpose_rejected_by_schema(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.post(
            f"{API}/auth/otp/request",
            json={"target_type": "phone", "target": unique_phone(), "purpose": "mfa"},
        )
    assert r.status_code == 422


async def test_verify_malformed_phone_target_rejected_by_schema(db):
    """Regression: a short digit-only target must 422 before reaching the
    service/audit layer, where an unmasked target could leak into the audit log."""
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.post(
            f"{API}/auth/otp/verify",
            json={"target": "901234567", "code": "000000", "purpose": "phone_verify"},
        )
    assert r.status_code == 422


async def test_request_does_not_send_inline(db):
    """Task 4: request_otp only enqueues to the outbox; nothing reaches the
    sender until a worker (deliver_one) drains the queue."""
    sender = get_otp_sender()
    assert isinstance(sender, MockOtpSender)
    before = len(sender.sent)
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.post(
            f"{API}/auth/otp/request",
            json={"target_type": "phone", "target": unique_phone(), "purpose": "phone_verify"},
        )
        assert r.status_code == 204
    assert len(sender.sent) == before  # nothing sent until the worker delivers
    assert await integrations_service.deliver_one(db) is True
    assert len(sender.sent) == before + 1


async def test_code_hash_is_hmac(db):
    """Task 4: 6-digit codes are hashed with the keyed hash_otp, not plain sha256
    (a bare sha256 of a 6-digit space is brute-forceable from a DB leak)."""
    phone = unique_phone()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.post(
            f"{API}/auth/otp/request",
            json={"target_type": "phone", "target": phone, "purpose": "phone_verify"},
        )
        assert r.status_code == 204
    code = await _delivered_code(db)
    row = await repo.latest_pending_otp(db, target=phone, purpose="phone_verify")
    assert row is not None
    assert row.code_hash == hash_otp(code)


def test_mask_target_short_non_email_never_echoes_full_value():
    masked = _mask_target("901234567")
    assert "901234567" not in masked
    assert masked == "***4567"


def test_mask_target_full_phone():
    assert _mask_target("+998901234567") == "+99890***4567"


def test_mask_target_email():
    assert _mask_target("user@example.com") == "u***@example.com"
