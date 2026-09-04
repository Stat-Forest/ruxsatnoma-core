"""Task 3: suspend and resume — the first pair of acts riding on `decide()`.

`test_decisions.py` already proves the signed-decision mechanism itself
(ruling 1's "suspended twice in its life", the refused-signature rollback, the
zone gate ahead of the signer check). This file proves the two ROUTES'
product-facing behaviour: the per-act document requirement, the timeline row
`decide()` writes, and the two things a suspension makes reachable for the
first time — the public page's «тўхтатилган» and `add_signature`'s own
`not_pending_signatures` refusal.
"""

from app.modules.permits import repo
from tests.modules.permits.conftest import sign_decision, sign_permit


async def test_suspension_needs_its_supporting_document(
    active_permit, head_client, suspend_reason_id
) -> None:
    """Ruling 6, and С13's «+ подтверждающий документ»."""
    refused = await sign_decision(
        head_client, active_permit.id, "suspend", reason_item_id=suspend_reason_id, doc_file_id=None
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["reason"] == "doc_file_required"


async def test_a_resume_needs_none(
    active_permit, head_client, suspend_reason_id, resume_reason_id, order_file_id
) -> None:
    await sign_decision(
        head_client,
        active_permit.id,
        "suspend",
        reason_item_id=suspend_reason_id,
        doc_file_id=order_file_id,
    )
    resumed = await sign_decision(
        head_client, active_permit.id, "resume", reason_item_id=resume_reason_id
    )
    assert resumed.status_code == 200, resumed.text


async def test_the_timeline_carries_the_ground_and_the_order(
    db, active_permit, head_client, suspend_reason_id, order_file_id
) -> None:
    """`permit_status_history` has had `reason_item_id`, `legal_basis` and
    `doc_file_id` since 0019 and this is the first writer of all three."""
    await sign_decision(
        head_client,
        active_permit.id,
        "suspend",
        reason_item_id=suspend_reason_id,
        doc_file_id=order_file_id,
        legal_basis="Лесхоз буйруғи №7",
    )
    rows = await repo.status_history(db, active_permit.id)
    last = rows[-1]
    assert (last.from_status, last.to_status) == ("active", "suspended")
    assert last.reason_item_id == suspend_reason_id
    assert last.doc_file_id == order_file_id
    assert last.legal_basis == "Лесхоз буйруғи №7"


async def test_a_suspended_permit_reads_toxtatilgan_on_the_public_page(
    client, active_permit, head_client, suspend_reason_id, order_file_id
) -> None:
    """`PUBLIC_STATUS_LABELS` has carried «тўхтатилган» since 3.11a with nothing
    able to reach it; this is the request that reaches it."""
    await sign_decision(
        head_client,
        active_permit.id,
        "suspend",
        reason_item_id=suspend_reason_id,
        doc_file_id=order_file_id,
    )
    page = await client.get(
        "/api/v1/public/permits/check",
        params={"series": active_permit.series, "number": active_permit.number},
    )
    assert page.status_code == 200
    assert page.json()["status"] == "тўхтатилган"


async def test_a_suspended_permit_takes_no_further_signatures(
    active_permit, head_client, holder_client, permit_pdf, suspend_reason_id, order_file_id
) -> None:
    """`add_signature` refuses anything that is not `pending_signatures`, which
    is what stops a suspended permit collecting signatures as if nothing had
    happened — the sentence 3.11a wrote and this stage makes reachable."""
    await sign_decision(
        head_client,
        active_permit.id,
        "suspend",
        reason_item_id=suspend_reason_id,
        doc_file_id=order_file_id,
    )
    refused = await sign_permit(holder_client, active_permit.id, "permit_recipient", permit_pdf)
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["reason"] == "not_pending_signatures"


async def test_legal_basis_has_a_length_cap(
    active_permit, head_client, suspend_reason_id, order_file_id
) -> None:
    """`legal_basis` is written to the append-only `permit_status_history` row,
    so the cap (`max_length=2000`) is a schema-level refusal, before anything is
    ever handed to `sign()`."""
    refused = await head_client.client.post(
        f"/api/v1/permits/{active_permit.id}/suspend",
        json={
            "reason_item_id": str(suspend_reason_id),
            "legal_basis": "x" * 2001,
            "doc_file_id": str(order_file_id),
            "pkcs7": "irrelevant",
        },
    )
    # `ERR-VAL-001` from FastAPI's own request-validation handler, naming the
    # field — never `ERR-SIGN-001`, which an invalid `pkcs7` would also produce
    # at 422 and which a looser assertion could not tell apart from this cap.
    assert refused.status_code == 422
    body = refused.json()["error"]
    assert body["code"] == "ERR-VAL-001"
    assert any(err["loc"][-1] == "legal_basis" for err in body["details"]["errors"])
