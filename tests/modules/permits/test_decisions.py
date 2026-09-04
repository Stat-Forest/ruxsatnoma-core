from app.modules.permits import decisions, repo, service
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
