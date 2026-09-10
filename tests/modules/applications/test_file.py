"""Stage 12, B4: `POST /applications` files the whole application in one
request and creates it SUBMITTED — the fourteen steps of `submit`, re-ordered
so nothing is pending when `sign()` runs (plan 12, R1/R2/R3)."""

import base64
import uuid

import pytest
from sqlalchemy import func, select

from app.core.errors import DomainError
from app.modules.applications.models import (
    Application,
    ApplicationCheck,
    ApplicationStatusHistory,
)
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.norms.models import Calculation
from app.modules.signatures.models import Signature
from tests.modules.applications.test_submit import _submit, _submit_with_button, _upload


async def _count(db, model, **where):
    stmt = select(func.count()).select_from(model)
    for column, value in where.items():
        stmt = stmt.where(getattr(model, column) == value)
    return await db.scalar(stmt)


def _over_limit(filing):
    return {**filing, "items": [{**filing["items"][0], "head_count": 100_000}]}


async def test_a_filing_creates_a_submitted_numbered_priced_signed_application(
    db, applicant_client, filing_ready_for_submission, hodim_user
):
    result = await _submit_with_button(applicant_client, filing_ready_for_submission)
    assert result.status_code == 201, result.text
    body = result.json()
    application_id = uuid.UUID(body["id"])
    assert body["status"] == "SUBMITTED"
    assert body["number"].startswith("RX-")
    assert body["submitted_at"] and body["sla_deadline_at"] and body["rules_accepted_at"]
    assert body["contour_version_id"] and body["requested_area_ha"]
    assert body["assigned_org_id"] is not None, "auto-assignment ran"
    assert await _count(db, Calculation, application_id=application_id) == 1
    assert await _count(db, ApplicationCheck, application_id=application_id) >= 1
    history = (
        (
            await db.execute(
                select(ApplicationStatusHistory).where(
                    ApplicationStatusHistory.application_id == application_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [(h.from_status, h.to_status) for h in history] == [(None, "SUBMITTED")]
    signature = (
        await db.execute(select(Signature).where(Signature.object_id == history[0].id))
    ).scalar_one()
    assert signature.kind == "simple" and signature.verification_status == "valid"


async def test_a_filing_carries_its_items_and_documents(
    db, applicant_client, filing_ready_for_submission, benefit_doc_type_item_id
):
    file_id = await _upload(applicant_client)
    filing = {
        **filing_ready_for_submission,
        "documents": [{"doc_type_item_id": str(benefit_doc_type_item_id), "file_id": file_id}],
    }
    result = await _submit_with_button(applicant_client, filing)
    assert result.status_code == 201, result.text
    card = (await applicant_client.get(f"/api/v1/applications/{result.json()['id']}")).json()
    assert [i["head_count"] for i in card["items"]] == [40]
    assert [d["file_id"] for d in card["documents"]] == [file_id]


async def test_a_legal_entity_files_with_the_package_it_signed(
    db, representative_client, legal_filing_ready_for_submission
):
    """R2 end to end: package → sign → file with the minted id; the row has it."""
    packaged = await representative_client.post(
        "/api/v1/applications/package", json=legal_filing_ready_for_submission
    )
    assert packaged.status_code == 200, packaged.text
    result = await _submit(representative_client, legal_filing_ready_for_submission)
    assert result.status_code == 201, result.text
    assert result.json()["status"] == "SUBMITTED"
    assert result.json()["on_behalf"] == "legal"


async def test_a_legal_entity_without_an_envelope_is_refused_before_anything_is_written(
    db, representative_client, legal_filing_ready_for_submission
):
    before = await _count(db, Application)
    result = await _submit_with_button(representative_client, legal_filing_ready_for_submission)
    assert result.status_code == 422, result.text
    assert result.json()["error"]["details"]["reason"] == "simple_signature_not_allowed"
    assert await _count(db, Application) == before


async def test_pkcs7_and_application_id_travel_together(
    applicant_client, filing_ready_for_submission
):
    without_id = await applicant_client.post(
        "/api/v1/applications",
        json={**filing_ready_for_submission, "rules_accepted": True, "pkcs7": "MIIB"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert without_id.status_code == 422, without_id.text
    assert without_id.json()["error"]["details"]["reason"] == "package_id_required"
    without_envelope = await applicant_client.post(
        "/api/v1/applications",
        json={
            **filing_ready_for_submission,
            "rules_accepted": True,
            "application_id": str(uuid.uuid4()),
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert without_envelope.status_code == 422, without_envelope.text
    assert without_envelope.json()["error"]["details"]["reason"] == "package_id_unexpected"


async def test_a_signature_over_another_package_cannot_file_under_a_taken_id(
    db, representative_client, legal_filing_ready_for_submission
):
    """A client that lies about `application_id` but signed the package the
    server minted meets `package_changed` — the bytes named another id — and
    no second row appears."""
    first = await _submit(representative_client, legal_filing_ready_for_submission)
    assert first.status_code == 201, first.text
    taken = first.json()["id"]
    pinfl = (await representative_client.get("/api/v1/auth/me")).json()["applicant"]["pinfl"]
    next_season = {
        **legal_filing_ready_for_submission,
        "period_from": "2028-05-01",
        "period_to": "2028-09-30",
    }
    packaged = (
        await representative_client.post("/api/v1/applications/package", json=next_season)
    ).json()
    forged = await representative_client.post(
        "/api/v1/applications",
        json={
            **next_season,
            "rules_accepted": True,
            "application_id": taken,
            "pkcs7": encode_mock_signature(
                document=base64.b64decode(packaged["package"]),
                serial=f"SER-{uuid.uuid4().hex[:12]}",
                issuer="ISS-TEST",
                pinfl=pinfl,
            ),
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert forged.status_code == 422, forged.text
    assert forged.json()["error"]["details"]["reason"] == "package_changed"
    assert await _count(db, Application, id=uuid.UUID(taken)) == 1


async def test_an_id_already_filed_is_refused_by_the_primary_key(
    db, representative_client, legal_filing_ready_for_submission
):
    """R2's own refusal: a package deliberately naming a filed id (the test
    seam on `package_filing`), signed for real, filed → 409 `already_filed`,
    one row."""
    from app.modules.applications import service
    from app.modules.applications.schemas import ApplicationFileIn, ApplicationFilingIn
    from app.modules.auth.models import User

    first = await _submit(representative_client, legal_filing_ready_for_submission)
    assert first.status_code == 201, first.text
    taken = uuid.UUID(first.json()["id"])
    me = (await representative_client.get("/api/v1/auth/me")).json()
    user = await db.get(User, uuid.UUID(me["user"]["id"]))
    assert user is not None
    next_season = {
        **legal_filing_ready_for_submission,
        "period_from": "2028-05-01",
        "period_to": "2028-09-30",
    }
    _, package = await service.package_filing(
        db, ApplicationFilingIn(**next_season), actor=user, application_id=taken
    )
    pkcs7 = encode_mock_signature(
        document=package,
        serial=f"SER-{uuid.uuid4().hex[:12]}",
        issuer="ISS-TEST",
        pinfl=me["applicant"]["pinfl"],
    )
    with pytest.raises(DomainError) as refused:
        await service.file(
            db,
            ApplicationFileIn(
                **next_season, rules_accepted=True, application_id=taken, pkcs7=pkcs7
            ),
            actor=user,
        )
    assert refused.value.code == "ERR-APP-004"
    assert refused.value.details == {"reason": "already_filed", "application_id": str(taken)}
    await db.rollback()
    assert await _count(db, Application, id=taken) == 1


async def test_a_blocking_check_refuses_the_filing_and_leaves_no_row(
    db, applicant_client, filing_ready_for_submission
):
    before = await _count(db, Application)
    result = await _submit_with_button(applicant_client, _over_limit(filing_ready_for_submission))
    assert result.status_code in (409, 422), result.text
    assert result.json()["error"]["code"].startswith("ERR-NORM-")
    assert await _count(db, Application) == before


async def test_a_filing_refused_by_a_blocking_check_leaves_an_audit_row_and_nothing_else(
    db, applicant_client, filing_ready_for_submission
):
    """R3: the refusal at step 6 audits `application.file` as denied and
    commits that row alone — no application, no checks, no signature."""
    from app.modules.audit.models import AuditLog

    audits_before = await _count(db, AuditLog, action="application.file")
    apps_before = await _count(db, Application)
    result = await _submit_with_button(applicant_client, _over_limit(filing_ready_for_submission))
    assert result.status_code in (409, 422), result.text
    assert await _count(db, AuditLog, action="application.file") == audits_before + 1
    denied = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action == "application.file")
                .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            )
        )
        .scalars()
        .first()
    )
    assert denied is not None and denied.result == "denied"
    assert await _count(db, Application) == apps_before


async def test_an_incomplete_filing_names_the_missing_fields(
    applicant_client, filing_ready_for_submission
):
    result = await _submit_with_button(
        applicant_client, {**filing_ready_for_submission, "period_to": None}
    )
    assert result.status_code == 400, result.text
    assert "period_to" in result.json()["error"]["details"]["missing"]


async def test_rules_must_be_accepted(applicant_client, filing_ready_for_submission):
    result = await _submit_with_button(
        applicant_client, filing_ready_for_submission, rules_accepted=False
    )
    assert result.status_code == 400, result.text
    assert "rules_accepted" in result.json()["error"]["details"]["missing"]


async def test_a_duplicate_filing_on_an_overlapping_period_names_the_first_number(
    applicant_client, filing_ready_for_submission, second_filing_same_contour
):
    first = await _submit_with_button(applicant_client, filing_ready_for_submission)
    assert first.status_code == 201, first.text
    second = await _submit_with_button(applicant_client, second_filing_same_contour)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "ERR-APP-002"
    assert second.json()["error"]["details"]["existing_number"] == first.json()["number"]


async def test_the_filing_route_requires_an_idempotency_key(
    applicant_client, filing_ready_for_submission
):
    result = await applicant_client.post(
        "/api/v1/applications", json={**filing_ready_for_submission, "rules_accepted": True}
    )
    assert result.status_code == 422, result.text
    assert result.json()["error"]["details"]["reason"] == "idempotency_key_required"


async def test_a_replayed_key_returns_the_stored_response_and_not_a_second_number(
    applicant_client, filing_ready_for_submission
):
    key = str(uuid.uuid4())
    first = await _submit_with_button(applicant_client, filing_ready_for_submission, key=key)
    again = await _submit_with_button(applicant_client, filing_ready_for_submission, key=key)
    assert first.status_code == again.status_code == 201, again.text
    assert first.json()["number"] == again.json()["number"]


async def test_a_stranger_cannot_file_in_my_name(
    other_applicant_client, legal_filing_ready_for_submission
):
    """The legal entity's applicant id in a body from a user holding no
    representation of it: refused with a domain reason, no row."""
    result = await _submit_with_button(
        other_applicant_client, {**legal_filing_ready_for_submission, "on_behalf": "self"}
    )
    assert result.status_code == 422, result.text
    assert result.json()["error"]["details"]["reason"] == "applicant_is_not_the_caller"
