"""Task 5: the duplicate (нусха) register.

`permit_duplicates` was created by migration 0023 with no writer (3.11a's own
module docstring) — this is that writer. The one rule worth re-stating: a
duplicate is the SAME stored bytes under a register row (`file_id ==
permit.pdf_file_id`), never a re-render, so its own QR is inherited unchanged
whether or not it happens to be correct (`service.issue_duplicate`).
"""

import hashlib

from app.modules.permits import repo, service


async def test_a_duplicate_points_at_the_same_bytes(db, active_permit, hodim_client) -> None:
    """Ruling 9. The whole register is a record of copies of ONE document."""
    created = await hodim_client.post(
        f"/api/v1/permits/{active_permit.id}/duplicates",
        json={"reason": "Асл нусха йўқолган"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["file_id"] == str(active_permit.pdf_file_id)

    row = (await repo.duplicates(db, active_permit.id))[0]
    assert row.file_id == active_permit.pdf_file_id
    assert row.reason == "Асл нусха йўқолган"


async def test_the_pdf_a_duplicate_names_still_verifies_against_doc_hash(
    db, active_permit, hodim_client
) -> None:
    """The reason (в) was refused: a re-rendered copy would not hash to
    `doc_hash` and all four ERI signatures would fail against it."""
    await hodim_client.post(f"/api/v1/permits/{active_permit.id}/duplicates", json={"reason": "х"})
    data = await service.pdf_bytes(db, active_permit.id)
    assert hashlib.sha256(data).hexdigest() == active_permit.doc_hash


async def test_an_unsigned_permit_has_no_duplicate(issued_permit, hodim_client) -> None:
    refused = await hodim_client.post(
        f"/api/v1/permits/{issued_permit.id}/duplicates", json={"reason": "х"}
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["reason"] == "not_duplicable"


async def test_a_permit_with_no_document_cannot_be_duplicated(
    db, active_permit, hodim_client
) -> None:
    """Defensive, not reachable today: `service.issue` sets `pdf_file_id` in the
    same INSERT that creates the permit row, so no real permit is ever without
    one. `pdf_file_id` stays nullable in the schema regardless, and the check
    must refuse cleanly rather than crash the day that stops being true."""
    active_permit.pdf_file_id = None
    await db.flush()
    refused = await hodim_client.post(
        f"/api/v1/permits/{active_permit.id}/duplicates", json={"reason": "х"}
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["reason"] == "no_document"


async def test_an_empty_reason_is_refused(active_permit, hodim_client) -> None:
    refused = await hodim_client.post(
        f"/api/v1/permits/{active_permit.id}/duplicates", json={"reason": "   "}
    )
    assert refused.status_code == 422


async def test_the_holder_sees_their_own_register_and_a_stranger_does_not(
    active_permit, hodim_client, holder_client, chief_forester_client, other_applicant_client
) -> None:
    await hodim_client.post(f"/api/v1/permits/{active_permit.id}/duplicates", json={"reason": "х"})
    mine = await holder_client.client.get(f"/api/v1/permits/{active_permit.id}/duplicates")
    assert mine.status_code == 200 and len(mine.json()) == 1

    # A required signer of THIS permit, in its own organization, holding
    # NEITHER `permits.issue` nor `permits.manage` (migration 0019). It reads
    # the register because `_readable_permit` admits it — the register's
    # audience is that function's, not the POST's two permissions (`e7df057`).
    signer = await chief_forester_client.client.get(
        f"/api/v1/permits/{active_permit.id}/duplicates"
    )
    assert signer.status_code == 200 and len(signer.json()) == 1

    theirs = await other_applicant_client.client.get(
        f"/api/v1/permits/{active_permit.id}/duplicates"
    )
    assert theirs.status_code == 404  # the same answer the card gives, not an oracle
