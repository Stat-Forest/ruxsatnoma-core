"""POST /api/v1/webhooks/eskiz/{secret}: an anonymous endpoint whose only guard is
the shared secret (ruling 18). A body we cannot interpret becomes a dead letter and
still answers 200 — a 4xx would make the provider retry it forever."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.main import create_app
from app.modules.integrations.models import InboundDeadLetter
from app.modules.notifications import service
from app.modules.notifications.models import Notification
from tests.conftest import make_client
from tests.modules.auth.test_sessions import make_user

SECRET = "test-callback-secret"
EVENT = "permit.active"  # keeps an active `sms` template after 0060 (ruling #211)


async def _sms_notification(db) -> Notification:
    user = await make_user(
        db, role_code="applicant", phone="998901234567", phone_verified_at=datetime.now(UTC)
    )
    rows = await service.notify(db, event_code=EVENT, recipient_user_id=user.id, params={})
    sms = next(r for r in rows if r.channel == "sms")
    sms.status = "sent"
    sms.sent_at = datetime.now(UTC)
    sms.provider_message_id = f"prov-{uuid.uuid4().hex[:8]}"
    await db.flush()
    return sms


async def _post(
    monkeypatch,
    body: dict,
    *,
    secret: str = SECRET,
    form: bool = False,
    files: dict | None = None,
):
    monkeypatch.setenv("ESKIZ_CALLBACK_SECRET", SECRET)
    from app.config import get_settings

    get_settings.cache_clear()
    async with make_client(create_app(), lifespan=True) as client:
        url = f"/api/v1/webhooks/eskiz/{secret}"
        if files is not None:
            response = await client.post(url, files=files)
        else:
            response = await (client.post(url, data=body) if form else client.post(url, json=body))
    get_settings.cache_clear()
    return response


async def test_delivered_report_marks_the_notification_delivered(db, monkeypatch):
    sms = await _sms_notification(db)
    await db.commit()
    r = await _post(monkeypatch, {"user_sms_id": str(sms.provider_reference), "status": "DELIVRD"})
    assert r.status_code == 200
    assert r.json()["result"] == "ok"
    await db.refresh(sms)
    assert sms.status == "delivered"
    assert sms.delivered_at is not None


async def test_correlation_falls_back_to_the_provider_id(db, monkeypatch):
    sms = await _sms_notification(db)
    await db.commit()
    r = await _post(monkeypatch, {"message_id": sms.provider_message_id, "status": "DELIVRD"})
    assert r.json()["result"] == "ok"
    await db.refresh(sms)
    assert sms.status == "delivered"


async def test_a_failure_report_marks_the_notification_failed(db, monkeypatch):
    sms = await _sms_notification(db)
    await db.commit()
    await _post(monkeypatch, {"user_sms_id": str(sms.provider_reference), "status": "REJECTD"})
    await db.refresh(sms)
    assert sms.status == "failed"
    assert sms.error == "REJECTD"


async def test_an_intermediate_status_leaves_the_row_alone(db, monkeypatch):
    sms = await _sms_notification(db)
    await db.commit()
    r = await _post(monkeypatch, {"user_sms_id": str(sms.provider_reference), "status": "STORED"})
    assert r.json()["result"] == "ignored"
    await db.refresh(sms)
    assert sms.status == "sent"


async def test_a_repeated_delivered_report_is_a_no_op(db, monkeypatch):
    sms = await _sms_notification(db)
    await db.commit()
    await _post(monkeypatch, {"user_sms_id": str(sms.provider_reference), "status": "DELIVRD"})
    r = await _post(monkeypatch, {"user_sms_id": str(sms.provider_reference), "status": "REJECTD"})
    assert r.json()["result"] == "ignored"  # terminal statuses are never overwritten
    await db.refresh(sms)
    assert sms.status == "delivered"


async def test_a_wrong_secret_is_a_404(db, monkeypatch):
    r = await _post(monkeypatch, {"user_sms_id": "1", "status": "DELIVRD"}, secret="nope")
    assert r.status_code == 404


async def test_an_unknown_reference_becomes_a_dead_letter(db, monkeypatch):
    unknown = "999999999999"  # the largest reference Eskiz accepts; names no row here
    r = await _post(monkeypatch, {"user_sms_id": unknown, "status": "DELIVRD"})
    assert r.status_code == 200
    assert r.json()["result"] == "dead_letter"
    letter = (
        (await db.execute(select(InboundDeadLetter).where(InboundDeadLetter.source == "eskiz")))
        .scalars()
        .all()
    )
    assert any(unknown in str(row.payload) for row in letter)


async def test_a_form_encoded_report_is_accepted(db, monkeypatch):
    sms = await _sms_notification(db)
    await db.commit()
    r = await _post(
        monkeypatch, {"user_sms_id": str(sms.provider_reference), "status": "DELIVRD"}, form=True
    )
    assert r.json()["result"] == "ok"


async def test_a_body_without_a_status_becomes_a_dead_letter(db, monkeypatch):
    sms = await _sms_notification(db)
    await db.commit()
    r = await _post(monkeypatch, {"user_sms_id": str(sms.provider_reference)})
    assert r.json()["result"] == "dead_letter"


async def test_a_multipart_file_part_becomes_a_dead_letter(db, monkeypatch):
    # A file part parses to an UploadFile, not a str — the one shape that must be
    # sanitized rather than merely forwarded, or it reaches the JSONB payload
    # column unencodable and a 500 replaces the dead letter (review finding).
    r = await _post(monkeypatch, {}, files={"file": ("hostname", b"forest", "text/plain")})
    assert r.status_code == 200
    assert r.json()["result"] == "dead_letter"
    letter = (
        (await db.execute(select(InboundDeadLetter).where(InboundDeadLetter.source == "eskiz")))
        .scalars()
        .all()
    )
    assert any(row.payload.get("file") == "<UploadFile>" for row in letter)


async def test_a_lowercase_status_is_normalized(db, monkeypatch):
    # Every other test here sends an already-uppercase status, so this is the only
    # one that would fail if apply_delivery_report's `.upper()` were ever deleted.
    sms = await _sms_notification(db)
    await db.commit()
    r = await _post(monkeypatch, {"user_sms_id": str(sms.provider_reference), "status": "delivrd"})
    assert r.json()["result"] == "ok"
    await db.refresh(sms)
    assert sms.status == "delivered"


async def test_a_non_ascii_secret_is_a_404_not_a_500(db, monkeypatch):
    """`secrets.compare_digest` raises TypeError on non-ASCII str operands, so a
    single odd path segment turned the one route whose stated invariant is "never
    500 on garbage" into a 500 (final whole-branch review of 3.5, finding 8)."""
    r = await _post(monkeypatch, {"status": "DELIVRD"}, secret="Ω")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_report_naming_a_uuid_is_a_dead_letter_not_a_crash(db, monkeypatch):
    """The 3.5 client sent the notification uuid as `user_sms_id`; Eskiz never
    accepted one (`400 user_sms_id is invalid`), so a report carrying a uuid is
    a body we cannot interpret — recorded, answered 200, never a 500."""
    sms = await _sms_notification(db)
    await db.commit()
    r = await _post(monkeypatch, {"user_sms_id": str(sms.id), "status": "DELIVRD"})
    assert r.status_code == 200
    assert r.json()["result"] == "dead_letter"
