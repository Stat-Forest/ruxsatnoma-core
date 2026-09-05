"""Task 4: revoke — the third act riding on `decide()`, and the one with a
side effect neither `suspend` nor `resume` carries.

`test_decisions.py` and `test_suspend.py` already prove the signed-decision
mechanism and the per-act document requirement; this file proves what is
specific to REVOKE: it is reachable from `suspended` as well as `active`
(`PERMIT_TRANSITIONS` allows both), it is terminal (a second revoke is a named
409, not a silent no-op), it feeds the nightly closure sweep `jobs.py` has
carried since 3.11a with nothing able to produce a `revoked` permit, and its
notification is this stage's WHOLE refund story (ruling 15 — no
`start_refund` anywhere in this module).
"""

import uuid

from app.modules.applications import service as applications_service
from app.modules.permits import events, jobs
from app.modules.permits.models import ForestTicket
from tests.modules.permits.conftest import notification_rows, sign_decision


async def test_revocation_closes_the_application_on_the_next_sweep(
    db, active_permit, head_client, revoke_reason_id, order_file_id
) -> None:
    """`jobs.FINISHED_STATUSES` has carried `revoked` since 3.11a with nothing
    able to produce one."""
    await sign_decision(
        head_client,
        active_permit.id,
        "revoke",
        reason_item_id=revoke_reason_id,
        doc_file_id=order_file_id,
    )
    application = await applications_service.get(db, active_permit.application_id)
    assert application is not None
    # `active_permit`'s own fixture chain loaded this row on `db` while it was
    # still `PAID`; `expire_on_commit=False` means neither side's commit
    # refreshes it, and `get`'s `db.get` hits the identity map rather than
    # issuing a SELECT (the same lesson `test_signatures.py::_reread`
    # documents twice already) — so the move to `PERMIT_ISSUED` the ACTIVATION
    # made on a different session needs an explicit re-read to become visible
    # here.
    await db.refresh(application)
    assert application.status == "PERMIT_ISSUED"

    await jobs.close_finished(db)
    await db.refresh(application)
    assert application.status == "CLOSED"


async def test_a_suspended_permit_may_be_revoked(
    active_permit, head_client, suspend_reason_id, revoke_reason_id, order_file_id
) -> None:
    await sign_decision(
        head_client,
        active_permit.id,
        "suspend",
        reason_item_id=suspend_reason_id,
        doc_file_id=order_file_id,
    )
    revoked = await sign_decision(
        head_client,
        active_permit.id,
        "revoke",
        reason_item_id=revoke_reason_id,
        doc_file_id=order_file_id,
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"


async def test_revoking_twice_is_a_named_conflict_not_a_silent_success(
    active_permit, head_client, revoke_reason_id, order_file_id
) -> None:
    """`from == to` is how a retrying client tells «already applied» from a
    mistake (`PERMIT_TRANSITIONS` has no self-loop anywhere)."""
    await sign_decision(
        head_client,
        active_permit.id,
        "revoke",
        reason_item_id=revoke_reason_id,
        doc_file_id=order_file_id,
    )
    again = await sign_decision(
        head_client,
        active_permit.id,
        "revoke",
        reason_item_id=revoke_reason_id,
        doc_file_id=order_file_id,
    )
    assert again.status_code == 409
    details = again.json()["error"]["details"]
    assert (details["from"], details["to"]) == ("revoked", "revoked")


async def test_the_holder_is_told_and_the_text_mentions_the_refund(
    db, active_permit, head_client, revoke_reason_id, order_file_id
) -> None:
    """Ruling 15: the notification is the whole of this stage's refund handling.

    The word checked here is migration 0023's own — «қайтарим» (refund), not
    the deverbal «қайтариш» (to revert/undo) that would also fit the English
    gloss "the text mentions the refund" but does not appear in the seeded
    `permit.revoked` body. Asserting the word actually seeded keeps this test
    honest about what a holder reads, rather than about a word nothing writes.
    """
    await sign_decision(
        head_client,
        active_permit.id,
        "revoke",
        reason_item_id=revoke_reason_id,
        doc_file_id=order_file_id,
    )
    rows = await notification_rows(db, object_id=active_permit.id)
    revoked = [row for row in rows if row.event_code == events.PERMIT_REVOKED]
    assert revoked, "migration 0023 must seed permit.revoked, or notify() writes a fallback"
    assert "қайтарим" in revoked[0].rendered_text


async def test_revocation_also_revokes_the_permits_live_forest_ticket(
    db, active_permit, head_client, revoke_reason_id, order_file_id
) -> None:
    """Ruling 14 — the whole reason this act is bigger than `suspend`'s or
    `resume`'s. Built by hand through the ORM: Task 6's own issuance route
    does not exist yet, the same idiom `make_permit_on_contour` already uses
    for a row nothing can issue through a service."""
    ticket = ForestTicket(
        number=f"CHT-{uuid.uuid4()}",
        permit_id=active_permit.id,
        valid_from=active_permit.period_from,
        valid_to=active_permit.period_to,
        restrictions={},
        status="active",
        issued_by=head_client.user.id,
    )
    db.add(ticket)
    await db.flush()

    revoked = await sign_decision(
        head_client,
        active_permit.id,
        "revoke",
        reason_item_id=revoke_reason_id,
        doc_file_id=order_file_id,
    )
    assert revoked.status_code == 200, revoked.text

    await db.refresh(ticket)
    assert ticket.status == "revoked"
