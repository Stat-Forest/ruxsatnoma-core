"""The 3+1 ERI signatures, and the two checks stage 3.8 left to this stage.

`require_complete` matches purpose STRINGS only — it never asks whether the
person who signed `permit_head` holds that role, and 3.8 said so in its own
docstring, naming `signer_not_authorized` and deferring it here. Without that
check one person with one certificate signs all four lines and the permit reads
complete; this file is where that stops being possible.

Two deviations from the brief's verbatim tests, both forced by the shared,
persistent test database (lesson):

  * the ERI identity travels on the `Signer` fixture instead of being written
    as a literal at each call site — `users.pinfl` is UNIQUE, `certificates` is
    unique on `(serial_number, issuer)`, and these clients COMMIT, so a fixed
    `pinfl="11111111111111"` passes exactly once per freshly migrated database;
  * the recipient's fixture is `holder_client`, not `applicant_client`: the
    latter is re-exported from `tests/modules/gis/conftest.py` as an applicant
    unrelated to any application here, and `test_issue.py` needs it to stay
    that way. The 4th signature belongs to the permit's OWN holder.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications import service as applications_service
from app.modules.applications.models import Application
from app.modules.audit.models import AuditLog
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.notifications.models import Notification
from app.modules.permits import events, service, signers
from app.modules.permits.models import Permit, PermitStatusHistory
from tests.modules.permits.conftest import Signer

API = "/api/v1"


async def _sign(signer: Signer, permit_id: uuid.UUID, purpose: str, pdf: bytes):
    """One signature attempt over the stored document, with the signer's own
    ERI identity (see this file's docstring for why it is not a literal)."""
    return await signer.client.post(
        f"{API}/permits/{permit_id}/signatures",
        json={
            "purpose": purpose,
            "pkcs7": encode_mock_signature(
                document=pdf, serial=signer.serial, issuer="ISS-1", pinfl=signer.pinfl
            ),
        },
    )


# --- the map itself ----------------------------------------------------------


def test_the_purpose_role_map_is_the_four_of_ruling_4_and_nothing_else():
    """`signers` is pure — no session, no permit — so the map that decides who
    may sign what is checkable on its own, without an issuance in front of it."""
    assert signers.required_role("permit_head") == "executor_head"
    assert signers.required_role("permit_chief_forester") == "chief_forester"
    assert signers.required_role("permit_accountant") == "accountant"
    # The recipient is not a ROLE: `applicant` is held by every citizen in the
    # country, so the proof is owning the application, not holding a role.
    assert signers.required_role(signers.RECIPIENT_PURPOSE) is None
    # Fails closed: `permit_required_signatures` is admin-editable, so a purpose
    # with no entry here maps to no role, and no role is a refusal, not a pass.
    assert signers.required_role("permit_typo") is None
    assert set(signers.PURPOSE_ROLES) == {
        "permit_head",
        "permit_chief_forester",
        "permit_accountant",
        "permit_recipient",
    }


# --- the happy path ----------------------------------------------------------


async def test_four_signatures_make_the_permit_active(
    db: AsyncSession,
    issued_permit: Permit,
    permit_pdf: bytes,
    head_client: Signer,
    chief_forester_client: Signer,
    accountant_client: Signer,
    holder_client: Signer,
):
    """С11: ACTIVE only when all 3+1 are in. Not before, whatever the order —
    signatures may be taken in any order (plan ruling 5); `missing_purposes`
    returns a display order for a UI, never a gate."""
    for signer, purpose in (
        (accountant_client, "permit_accountant"),
        (head_client, "permit_head"),
        (chief_forester_client, "permit_chief_forester"),
    ):
        result = await _sign(signer, issued_permit.id, purpose, permit_pdf)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "pending_signatures"

    final = await _sign(holder_client, issued_permit.id, "permit_recipient", permit_pdf)
    assert final.status_code == 200, final.text
    assert final.json()["status"] == "active"
    assert final.json()["missing_signatures"] == []


async def test_each_signature_names_exactly_what_is_still_missing(
    issued_permit: Permit, permit_pdf: bytes, head_client: Signer
):
    """The response is what a signing UI drives off, so it must shrink by
    exactly the purpose just signed and keep the configured order."""
    result = await _sign(head_client, issued_permit.id, "permit_head", permit_pdf)
    assert result.status_code == 200, result.text
    assert result.json()["missing_signatures"] == [
        "permit_chief_forester",
        "permit_accountant",
        "permit_recipient",
    ]


async def test_the_last_signature_moves_the_application_to_permit_issued(
    db: AsyncSession,
    issued_permit: Permit,
    permit_pdf: bytes,
    holder_client: Signer,
    head_client: Signer,
    chief_forester_client: Signer,
    accountant_client: Signer,
):
    """Ruling 18: the application moves HERE, not at issuance. `tz/05` defines
    PERMIT_ISSUED as «сформировано и подписано», so before the last signature it
    is still PAID.

    Read through `applications.service.get`, never `GET /applications/{id}` —
    that route does not exist on this branch (ruling P2)."""
    for signer, purpose in (
        (head_client, "permit_head"),
        (chief_forester_client, "permit_chief_forester"),
        (accountant_client, "permit_accountant"),
    ):
        await _sign(signer, issued_permit.id, purpose, permit_pdf)

    application = await applications_service.get(db, issued_permit.application_id)
    assert application is not None
    assert application.status == "PAID"

    await _sign(holder_client, issued_permit.id, "permit_recipient", permit_pdf)

    # `refresh`, not a second `get`: the read above put the row in this session's
    # identity map and the session is `expire_on_commit=False`, so `get` would
    # hand back the cached PAID object rather than what the app's own connection
    # has since committed (lesson: the row in memory is not what Postgres
    # stored). Refreshing only THIS row also leaves `issued_permit` loaded —
    # `expire_all()` makes its next attribute access do sync IO and raise
    # MissingGreenlet.
    await db.refresh(application)
    assert application.status == "PERMIT_ISSUED"


async def test_activation_stamps_issued_at_and_leaves_a_timeline_and_a_notice(
    db: AsyncSession,
    issued_permit: Permit,
    permit_pdf: bytes,
    holder_client: Signer,
    head_client: Signer,
    chief_forester_client: Signer,
    accountant_client: Signer,
):
    """`issued_at` is the moment the permit came into force, not the moment the
    document was formed (`models.py`: ruling 18) — so it is null until here."""
    assert issued_permit.issued_at is None
    for signer, purpose in (
        (head_client, "permit_head"),
        (chief_forester_client, "permit_chief_forester"),
        (accountant_client, "permit_accountant"),
        (holder_client, "permit_recipient"),
    ):
        assert (await _sign(signer, issued_permit.id, purpose, permit_pdf)).status_code == 200

    await db.refresh(issued_permit)
    assert issued_permit.status == "active"
    assert issued_permit.issued_at is not None
    assert issued_permit.issued_at <= datetime.now(UTC)

    history = (
        await db.scalars(
            select(PermitStatusHistory).where(PermitStatusHistory.permit_id == issued_permit.id)
        )
    ).all()
    assert [(row.from_status, row.to_status) for row in history] == [
        (None, "pending_signatures"),
        ("pending_signatures", "active"),
    ]

    notices = (
        await db.scalars(
            select(Notification).where(
                Notification.object_id == issued_permit.id,
                Notification.event_code == events.PERMIT_ACTIVE,
            )
        )
    ).all()
    assert notices, "the holder is told their permit is in force"


# --- ruling 4: who may sign what ---------------------------------------------


async def test_the_accountant_cannot_sign_as_the_head(
    accountant_client: Signer, issued_permit: Permit, permit_pdf: bytes
):
    """Ruling 4 — the check stage 3.8 documented and deliberately left here:
    `require_complete` matches purpose STRINGS only, so without this one person
    with one certificate signs all four and the permit reads complete."""
    result = await _sign(accountant_client, issued_permit.id, "permit_head", permit_pdf)
    assert result.status_code == 403
    assert result.json()["error"]["details"]["reason"] == "signer_not_authorized"


async def test_a_head_from_another_leshoz_cannot_sign(
    other_org_head_client: Signer, issued_permit: Permit, permit_pdf: bytes
):
    """Holding the role is not enough — design/03 says users OF THE SAME
    ORGANIZATION."""
    result = await _sign(other_org_head_client, issued_permit.id, "permit_head", permit_pdf)
    assert result.status_code == 403
    assert result.json()["error"]["details"]["reason"] == "signer_not_authorized"


async def test_a_stranger_cannot_sign_as_the_recipient(
    other_applicant_client: Signer, issued_permit: Permit, permit_pdf: bytes
):
    """The recipient purpose is proven by owning the APPLICATION, which
    `sign()`'s own PINFL check cannot see: a different citizen signing with
    their own genuine certificate is crypto-valid and still not the holder."""
    result = await _sign(other_applicant_client, issued_permit.id, "permit_recipient", permit_pdf)
    assert result.status_code == 403
    assert result.json()["error"]["details"]["reason"] == "signer_not_authorized"


async def test_the_superuser_is_not_a_signatory(
    sys_admin_client: Signer, issued_permit: Permit, permit_pdf: bytes
):
    """`sys_admin` passes every `require_permission` gate (decision #41 ruling 2).
    It must NOT pass this one: a permission answers "may this role do this at
    all", a signature answers "who personally attests to this document"."""
    result = await _sign(sys_admin_client, issued_permit.id, "permit_head", permit_pdf)
    assert result.status_code == 403
    assert result.json()["error"]["details"]["reason"] == "signer_not_authorized"


async def test_a_purpose_with_no_role_mapping_is_refused(
    db: AsyncSession,
    override_required_signatures,
    head_client: Signer,
    issued_permit: Permit,
    permit_pdf: bytes,
):
    """Ruling 4 fails closed: `permit_required_signatures` is admin-editable, so
    a typo must refuse rather than open a slot anybody can fill.

    The override goes through a fixture that COMMITS and then deletes the row:
    every client here commits `db` before each request, so a flush-only write
    (3.8's own `_override` idiom) becomes permanent whether or not the test
    wants it to — and a rewritten requirement set left behind in this shared
    database breaks every later reader of it (lesson)."""
    await override_required_signatures("permit_head,permit_typo")

    result = await _sign(head_client, issued_permit.id, "permit_typo", permit_pdf)
    assert result.status_code == 403
    assert result.json()["error"]["details"]["reason"] == "signer_not_authorized"

    # The journaled reason has to be `unknown_purpose`, not `wrong_role`. The
    # 403 alone cannot tell the fail-closed guard from its neighbour — an
    # unmapped purpose reaches the role comparison with `None` on one side and
    # is refused there too, by accident of no role code being None. Asserting
    # the reason is what makes the guard's removal visible.
    entry = (
        await db.scalars(
            select(AuditLog).where(
                AuditLog.object_id == issued_permit.id, AuditLog.action == service.PERMIT_SIGN
            )
        )
    ).one()
    assert (entry.new_value or {}).get("reason") == "unknown_purpose"


async def test_a_refused_signer_is_recorded_before_the_refusal_is_raised(
    db: AsyncSession, accountant_client: Signer, issued_permit: Permit, permit_pdf: bytes
):
    """Early-commit-on-denial (decision #40): the raise would otherwise roll the
    trail back together with the very exception it exists to explain. The
    specific reason lives in the journal, not in the response — the API says
    only `signer_not_authorized`."""
    await _sign(accountant_client, issued_permit.id, "permit_head", permit_pdf)

    entries = (
        await db.scalars(
            select(AuditLog).where(
                AuditLog.object_id == issued_permit.id, AuditLog.action == service.PERMIT_SIGN
            )
        )
    ).all()
    assert [e.result for e in entries] == ["denied"]
    assert entries[0].basis == "signer_not_authorized"
    assert (entries[0].new_value or {}).get("reason") == "wrong_role"
    assert (entries[0].new_value or {}).get("purpose") == "permit_head"


async def test_a_cross_organization_attempt_is_flagged_ri_12(
    db: AsyncSession, other_org_head_client: Signer, issued_permit: Permit, permit_pdf: bytes
):
    """tz/10 RI-12 — «попытка доступа вне территориальных полномочий», High,
    immediate. The right role in the wrong leshoz is exactly that; a wrong role
    in the right leshoz is not, so only this branch carries the indicator."""
    await _sign(other_org_head_client, issued_permit.id, "permit_head", permit_pdf)

    entries = (
        await db.scalars(
            select(AuditLog).where(
                AuditLog.object_id == issued_permit.id, AuditLog.action == service.PERMIT_SIGN
            )
        )
    ).all()
    assert [(e.extra or {}).get("risk_indicator") for e in entries] == ["RI-12"]


# --- refusals that leave the permit exactly as it was ------------------------


async def test_an_invalid_signature_leaves_the_permit_pending(
    db: AsyncSession, head_client: Signer, issued_permit: Permit
):
    result = await head_client.client.post(
        f"{API}/permits/{issued_permit.id}/signatures",
        json={"purpose": "permit_head", "pkcs7": "not-a-signature"},
    )
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "ERR-SIGN-001"

    await db.refresh(issued_permit)
    assert issued_permit.status == "pending_signatures"
    assert issued_permit.issued_at is None


async def test_an_already_active_permit_takes_no_further_signature(
    db: AsyncSession,
    issued_permit: Permit,
    permit_pdf: bytes,
    holder_client: Signer,
    head_client: Signer,
    chief_forester_client: Signer,
    accountant_client: Signer,
):
    """`pending_signatures` and nothing else. Without the status check the four
    slots are full, so `sign()` would answer ERR-SIGN-002 «already signed» —
    true of the purpose, and the wrong explanation for the permit."""
    for signer, purpose in (
        (head_client, "permit_head"),
        (chief_forester_client, "permit_chief_forester"),
        (accountant_client, "permit_accountant"),
        (holder_client, "permit_recipient"),
    ):
        await _sign(signer, issued_permit.id, purpose, permit_pdf)

    again = await _sign(head_client, issued_permit.id, "permit_head", permit_pdf)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ERR-PERM-001"


async def test_a_role_without_the_grant_is_refused_by_the_route_itself(
    non_signatory_client: Signer, issued_permit: Permit
):
    """The route still needs its own `permits.sign` gate: `gis_specialist` holds
    no such grant, so it never reaches the signatory check (lesson: a permission
    answers "at all", the signatory check answers "which line")."""
    result = await non_signatory_client.client.post(
        f"{API}/permits/{issued_permit.id}/signatures",
        json={"purpose": "permit_head", "pkcs7": "irrelevant"},
    )
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "ERR-ACL-001"


async def test_signing_an_unknown_permit_is_a_404(head_client: Signer):
    result = await head_client.client.post(
        f"{API}/permits/{uuid.uuid4()}/signatures",
        json={"purpose": "permit_head", "pkcs7": "irrelevant"},
    )
    assert result.status_code == 404


# --- ruling 3, from the other side -------------------------------------------


async def test_every_signature_covers_the_same_document(
    db: AsyncSession,
    issued_permit: Permit,
    permit_pdf: bytes,
    head_client: Signer,
    chief_forester_client: Signer,
):
    """3.8's `require_complete` does not check that the signatures share a
    doc_hash, so this stage guarantees there is only ever one document to sign:
    every signature is taken over `service.pdf_bytes`, never a re-render."""
    from app.modules.signatures import service as signatures_service

    await _sign(head_client, issued_permit.id, "permit_head", permit_pdf)
    await _sign(chief_forester_client, issued_permit.id, "permit_chief_forester", permit_pdf)

    rows = await signatures_service.get_for_object(
        db, object_type="permit", object_id=issued_permit.id
    )
    assert len({row.doc_hash for row in rows}) == 1
    assert rows[0].doc_hash == issued_permit.doc_hash


async def test_a_refusal_does_not_move_the_application(
    db: AsyncSession,
    issued_permit: Permit,
    permit_pdf: bytes,
    paid_application: Application,
    accountant_client: Signer,
):
    """Ruling 18 ties PERMIT_ISSUED to the LAST valid signature and to nothing
    else — and `sign()`'s refusal path COMMITS whatever is pending on the
    session it was handed, so "the refusal changed nothing else" is a fact worth
    asserting rather than assuming."""
    await _sign(accountant_client, issued_permit.id, "permit_head", permit_pdf)

    application = await applications_service.get(db, paid_application.id)
    assert application is not None
    assert application.status == "PAID"
