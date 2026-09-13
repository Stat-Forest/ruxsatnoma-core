"""Ruling #183 (docs/decisions.md; stage 10, track B1): a citizen acting for
THEMSELVES signs the permit's recipient line with a button, no envelope at
all — `on_behalf='legal'`, and every staff purpose, keep needing an ERI
envelope exactly as before.

`sign_permit_simple` (`conftest.py`) posts the body with `pkcs7` OMITTED
entirely, matching a real browser rather than `pkcs7: null` — the two are
NOT the same wire shape and only the omitted form is what `PermitSignIn.
pkcs7: str | None = None` is meant to accept from an honest client.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications import service as applications_service
from app.modules.permits.models import Permit
from app.modules.signatures import service as signatures_service
from app.modules.signatures.models import Signature
from tests.modules.permits.conftest import Signer, sign_permit, sign_permit_simple


@pytest.fixture(autouse=True)
async def _recipient_line(recipient_line_required: None) -> None:
    """Ruling #210 took the recipient line out of the default requirement set;
    every test here is about that line, so the whole module runs under the
    pre-#210 four-line override (`permit_recipient` is still a known purpose an
    operator may require)."""


async def test_a_citizens_holder_signature_with_no_envelope_activates_the_permit(
    db: AsyncSession,
    issued_permit: Permit,
    permit_pdf: bytes,
    holder_client: Signer,
    head_client: Signer,
    chief_forester_client: Signer,
    accountant_client: Signer,
):
    """The positive path this whole track exists for: `on_behalf='self'`
    (`paid_application`'s own default), the three staff signatures taken
    normally, then the holder presses the button — no `pkcs7` at all — and
    the permit still reaches `active` exactly as C11 requires."""
    for signer, purpose in (
        (head_client, "permit_head"),
        (chief_forester_client, "permit_chief_forester"),
        (accountant_client, "permit_accountant"),
    ):
        result = await sign_permit(signer, issued_permit.id, purpose, permit_pdf)
        assert result.status_code == 200, result.text

    final = await sign_permit_simple(holder_client, issued_permit.id, "permit_recipient")
    assert final.status_code == 200, final.text
    assert final.json()["status"] == "active"
    assert final.json()["missing_signatures"] == []

    await db.refresh(issued_permit)
    assert issued_permit.status == "active"

    rows = (
        await db.scalars(
            select(Signature).where(
                Signature.object_type == "permit",
                Signature.object_id == issued_permit.id,
                Signature.purpose == "permit_recipient",
            )
        )
    ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "simple"
    assert row.certificate_id is None
    assert row.signature_value == ""
    assert row.verification_status == "valid"
    assert row.verification["kind"] == "simple"
    assert row.verification["pinfl"] == holder_client.pinfl
    assert row.doc_hash == issued_permit.doc_hash, (
        "ruling 3: the simple signature covers the SAME stored bytes as every"
        " ERI one — sha256(document), never a literal"
    )

    # The NEXT screen: the permit card embeds the signature rows through
    # `PermitSignatureRow`, which carried `certificate_id: uuid.UUID` — a
    # simple row has none, and the card answered 500 while this route was
    # green (stage 10 integration finding). Read it the way the holder's
    # browser does, right after the button.
    card = await holder_client.client.get(f"/api/v1/permits/{issued_permit.id}")
    assert card.status_code == 200, card.text
    holder_rows = [r for r in card.json()["signatures"] if r["purpose"] == "permit_recipient"]
    assert holder_rows == [
        {**holder_rows[0], "kind": "simple", "certificate_id": None, "verification_status": "valid"}
    ]
    assert {r["kind"] for r in card.json()["signatures"] if r["purpose"] != "permit_recipient"} == {
        "eri"
    }


async def test_a_legal_entitys_holder_signature_needs_an_envelope(
    db: AsyncSession,
    legal_issued_permit: Permit,
    legal_representative_client: Signer,
):
    """`on_behalf='legal'` keeps needing ERI, whatever the button says —
    decision #9, narrowed by #183 to `on_behalf='self'` only."""
    result = await sign_permit_simple(
        legal_representative_client, legal_issued_permit.id, "permit_recipient"
    )
    assert result.status_code == 422
    body = result.json()
    assert body["error"]["code"] == "ERR-SIGN-001"
    assert body["error"]["details"]["reason"] == "simple_signature_not_allowed"

    await db.refresh(legal_issued_permit)
    assert legal_issued_permit.status == "pending_signatures", (
        "a refused attempt must leave the permit exactly as it was"
    )
    rows = (
        await db.scalars(
            select(Signature).where(
                Signature.object_type == "permit",
                Signature.object_id == legal_issued_permit.id,
            )
        )
    ).all()
    assert rows == [], "sign_simple must never have been reached — no row, not even an invalid one"


async def test_a_staff_signer_cannot_use_the_button(
    db: AsyncSession, issued_permit: Permit, head_client: Signer
):
    """Every staff purpose keeps needing an envelope — the button is the
    HOLDER's alone, never a shortcut for `executor_head`/`chief_forester`/
    `accountant`."""
    result = await sign_permit_simple(head_client, issued_permit.id, "permit_head")
    assert result.status_code == 422
    body = result.json()
    assert body["error"]["code"] == "ERR-SIGN-001"
    assert body["error"]["details"]["reason"] == "simple_signature_not_allowed"

    await db.refresh(issued_permit)
    assert issued_permit.status == "pending_signatures"


async def test_a_second_simple_signature_on_the_same_purpose_is_refused(
    db: AsyncSession, issued_permit: Permit, holder_client: Signer
):
    """The one-per-purpose duplicate rule (`uq_signatures_valid_purpose`)
    holds for a simple signature exactly as it does for an ERI one — the
    permit is left `pending_signatures` (only the recipient line is signed,
    the three staff ones are not), so this stays on the service-level
    pre-check (`_refuse_duplicate_purpose`), never reaching `ERR-PERM-001`."""
    first = await sign_permit_simple(holder_client, issued_permit.id, "permit_recipient")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "pending_signatures"

    second = await sign_permit_simple(holder_client, issued_permit.id, "permit_recipient")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ERR-SIGN-002"

    rows = (
        await db.scalars(
            select(Signature).where(
                Signature.object_type == "permit",
                Signature.object_id == issued_permit.id,
                Signature.purpose == "permit_recipient",
            )
        )
    ).all()
    assert len(rows) == 1, "the duplicate attempt writes no second row"


async def test_pkcs7_present_signs_the_recipient_line_with_eri_as_before(
    db: AsyncSession, issued_permit: Permit, permit_pdf: bytes, holder_client: Signer
):
    """With `pkcs7` present nothing changes for anyone (contract point 4) —
    a citizen who DOES present an envelope still gets an ordinary ERI row,
    never `kind='simple'`."""
    result = await sign_permit(holder_client, issued_permit.id, "permit_recipient", permit_pdf)
    assert result.status_code == 200, result.text

    row = (
        await db.scalars(
            select(Signature).where(
                Signature.object_type == "permit",
                Signature.object_id == issued_permit.id,
                Signature.purpose == "permit_recipient",
            )
        )
    ).one()
    assert row.kind == "eri"
    assert row.certificate_id is not None


async def test_get_signatures_kind_simple_filters_to_the_button_only(
    db: AsyncSession,
    issued_permit: Permit,
    permit_pdf: bytes,
    holder_client: Signer,
    head_client: Signer,
):
    """`GET /signatures?kind=simple` (ruling #183) — one of each kind on the
    same object, filtered from the real HTTP route."""
    await sign_permit(head_client, issued_permit.id, "permit_head", permit_pdf)
    await sign_permit_simple(holder_client, issued_permit.id, "permit_recipient")

    only_simple = await holder_client.client.get(
        f"/api/v1/signatures?object_type=permit&object_id={issued_permit.id}&kind=simple"
    )
    assert only_simple.status_code == 200, only_simple.text
    items = only_simple.json()["items"]
    assert [item["kind"] for item in items] == ["simple"]
    assert items[0]["purpose"] == "permit_recipient"

    unfiltered = await holder_client.client.get(
        f"/api/v1/signatures?object_type=permit&object_id={issued_permit.id}"
    )
    assert {item["kind"] for item in unfiltered.json()["items"]} == {"eri", "simple"}


async def test_reverify_of_a_simple_row_returns_it_unchanged(
    db: AsyncSession, issued_permit: Permit, holder_client: Signer
):
    """`reverify` on a `kind='simple'` row (ruling #183): nothing cryptographic
    was ever checked, so this is a no-op that answers the SAME row, not an
    error and not a new evidence row — driven at the service level, since
    `POST /signatures/{id}/reverify` needs `signatures.reverify`, which no
    fixture in this file holds."""
    await sign_permit_simple(holder_client, issued_permit.id, "permit_recipient")
    original = (
        await db.scalars(
            select(Signature).where(
                Signature.object_type == "permit",
                Signature.object_id == issued_permit.id,
                Signature.purpose == "permit_recipient",
            )
        )
    ).one()
    assert original.kind == "simple"

    result = await signatures_service.reverify(
        db, signature_id=original.id, user=holder_client.user
    )
    assert result.id == original.id
    assert result.verification_status == "valid"
    assert result.kind == "simple"

    rows = (
        await db.scalars(
            select(Signature).where(
                Signature.object_type == "permit",
                Signature.object_id == issued_permit.id,
                Signature.purpose == "permit_recipient",
            )
        )
    ).all()
    assert len(rows) == 1, "reverify of a simple row must write no new record"


async def test_a_refusal_does_not_move_the_application(
    db: AsyncSession,
    legal_issued_permit: Permit,
    legal_representative_client: Signer,
):
    """The legal-entity refusal above leaves the application exactly where it
    was — `sign_simple` is never even reached, so there is nothing for it to
    have committed early (contrast `signatures.service.sign()`'s own early-
    commit refusals, which DO leave evidence behind by design)."""
    await sign_permit_simple(
        legal_representative_client, legal_issued_permit.id, "permit_recipient"
    )
    application = await applications_service.get(db, legal_issued_permit.application_id)
    assert application is not None
    await db.refresh(application)
    assert application.status == "PAID"


def test_signature_out_schema_carries_kind() -> None:
    """A schema-shape guard, cheap insurance against `SignatureOut` silently
    losing the field this whole stage adds — `test_api.py` (signatures) does
    not otherwise assert on the raw field set."""
    from app.modules.signatures.schemas import SignatureOut

    assert "kind" in SignatureOut.model_fields
