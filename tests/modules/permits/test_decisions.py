from app.modules.admin import repo as admin_repo
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.permits import decisions, repo, service
from app.modules.permits.schemas import DecisionIn
from app.modules.signatures import service as signatures_service
from tests.modules.permits.conftest import sign_decision


async def test_a_permit_can_be_suspended_twice_in_its_life(
    db, active_permit, head_client, suspend_reason_id, resume_reason_id, order_file_id
) -> None:
    """Ruling 1. Signing against the permit would make the SECOND suspension
    collide on `uq_signatures_valid_purpose` and a permit could be suspended
    exactly once, for ever."""
    first = await sign_decision(
        head_client,
        active_permit.id,
        "suspend",
        reason_item_id=suspend_reason_id,
        doc_file_id=order_file_id,
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "suspended"

    back = await sign_decision(
        head_client, active_permit.id, "resume", reason_item_id=resume_reason_id
    )
    assert back.status_code == 200, back.text
    assert back.json()["status"] == "active"

    again = await sign_decision(
        head_client,
        active_permit.id,
        "suspend",
        reason_item_id=suspend_reason_id,
        doc_file_id=order_file_id,
    )
    assert again.status_code == 200, again.text


async def test_a_refused_signature_leaves_the_permit_untouched(
    db, active_permit, head_client, suspend_reason_id, order_file_id
) -> None:
    """Ruling 3: `sign()` commits the caller's whole session before it raises, so
    a status change written first would be committed by its own refusal."""
    refused = await head_client.client.post(
        f"/api/v1/permits/{active_permit.id}/suspend",
        json={
            "reason_item_id": str(suspend_reason_id),
            "legal_basis": "буйруқ №7",
            "doc_file_id": str(order_file_id),
            "pkcs7": "not-a-signature",
        },
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "ERR-SIGN-001"

    permit = await service.get(db, active_permit.id)
    assert permit is not None and permit.status == "active"
    assert len(await repo.status_history(db, active_permit.id)) == 2  # created + activated


async def test_the_head_of_another_leshoz_may_not_decide(
    db, active_permit, other_org_head_client, suspend_reason_id, order_file_id
) -> None:
    """`decide()`'s step 3 (`_assert_organization_in_zone`) runs before its
    step 6 (the signer-identity check): a head of a wholly different leshoz
    fails the coarser zone gate first, so the finer-grained role/organization
    comparison behind `sign()` is never reached."""
    refused = await sign_decision(
        other_org_head_client,
        active_permit.id,
        "suspend",
        reason_item_id=suspend_reason_id,
        doc_file_id=order_file_id,
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "ERR-ACL-002"


async def test_the_signed_bytes_name_what_was_decided(db, active_permit) -> None:
    """Ruling 3(а): deterministic, and different for a different act."""
    suspend = decisions.decision_document(
        permit=active_permit,
        to_status="suspended",
        reason_code="PS-01",
        legal_basis="акт №5",
        doc_file_id=None,
    )
    revoke = decisions.decision_document(
        permit=active_permit,
        to_status="revoked",
        reason_code="PS-01",
        legal_basis="акт №5",
        doc_file_id=None,
    )
    assert suspend != revoke
    assert suspend == decisions.decision_document(
        permit=active_permit,
        to_status="suspended",
        reason_code="PS-01",
        legal_basis="акт №5",
        doc_file_id=None,
    )
    assert b"PS-01" in suspend and b"suspended" in suspend


async def test_the_signature_hangs_on_the_history_row_it_explains(
    db, active_permit, head_client, suspend_reason_id, order_file_id
) -> None:
    """Ruling 1: `object_id` is the timeline entry, minted server-side."""
    await sign_decision(
        head_client,
        active_permit.id,
        "suspend",
        reason_item_id=suspend_reason_id,
        doc_file_id=order_file_id,
    )
    last = (await repo.status_history(db, active_permit.id))[-1]
    signed = await signatures_service.get_for_object(
        db, object_type=decisions.DECISION_OBJECT_TYPE, object_id=last.id
    )
    assert [row.purpose for row in signed] == [decisions.DECISION_PURPOSE]
    assert signed[0].verification_status == "valid"


async def test_the_service_wrapper_produces_the_same_decision_as_the_route(
    db, active_permit, head_client, suspend_reason_id, resume_reason_id, order_file_id
) -> None:
    """Ruling 4, option а: `service.suspend`/`service.resume` are the path a
    caller OUTSIDE this module's own routes takes (stage 4.1's own actor), and
    must produce exactly what `POST /permits/{id}/suspend`/`/resume` produce —
    the same status change, the same `permit_status_history` row, the same
    signature anchored to it. Calls the SERVICE functions directly, never the
    HTTP routes, so a regression confined to the wrapper could not hide behind
    `test_suspend.py`'s route-level coverage."""
    suspend_item = await admin_repo.get_classifier_item(db, suspend_reason_id)
    assert suspend_item is not None
    suspend_document = decisions.decision_document(
        permit=active_permit,
        to_status="suspended",
        reason_code=suspend_item.code,
        legal_basis="Инспекция далолатномаси",
        doc_file_id=order_file_id,
    )
    suspended = await service.suspend(
        db,
        active_permit.id,
        data=DecisionIn(
            reason_item_id=suspend_reason_id,
            legal_basis="Инспекция далолатномаси",
            doc_file_id=order_file_id,
            pkcs7=encode_mock_signature(
                document=suspend_document,
                serial=head_client.serial,
                issuer="ISS-1",
                pinfl=head_client.pinfl,
            ),
        ),
        actor=head_client.user,
    )
    assert suspended.status == "suspended"

    rows = await repo.status_history(db, active_permit.id)
    last = rows[-1]
    assert (last.from_status, last.to_status) == ("active", "suspended")
    assert last.reason_item_id == suspend_reason_id
    assert last.doc_file_id == order_file_id
    assert last.legal_basis == "Инспекция далолатномаси"

    signed = await signatures_service.get_for_object(
        db, object_type=decisions.DECISION_OBJECT_TYPE, object_id=last.id
    )
    assert [row.purpose for row in signed] == [decisions.DECISION_PURPOSE]
    assert signed[0].verification_status == "valid"

    # And back, through `service.resume` — the same wrapper shape, the other act.
    resume_item = await admin_repo.get_classifier_item(db, resume_reason_id)
    assert resume_item is not None
    resume_document = decisions.decision_document(
        permit=suspended,
        to_status="active",
        reason_code=resume_item.code,
        legal_basis=None,
        doc_file_id=None,
    )
    resumed = await service.resume(
        db,
        active_permit.id,
        data=DecisionIn(
            reason_item_id=resume_reason_id,
            pkcs7=encode_mock_signature(
                document=resume_document,
                serial=head_client.serial,
                issuer="ISS-1",
                pinfl=head_client.pinfl,
            ),
        ),
        actor=head_client.user,
    )
    assert resumed.status == "active"

    resumed_row = (await repo.status_history(db, active_permit.id))[-1]
    assert (resumed_row.from_status, resumed_row.to_status) == ("suspended", "active")
    resumed_signed = await signatures_service.get_for_object(
        db, object_type=decisions.DECISION_OBJECT_TYPE, object_id=resumed_row.id
    )
    assert [row.purpose for row in resumed_signed] == [decisions.DECISION_PURPOSE]
    assert resumed_signed[0].verification_status == "valid"
