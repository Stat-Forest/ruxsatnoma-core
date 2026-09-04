"""Attaching and detaching an application's documents (plan 03.9a task 4).

A `file_id` an applicant supplies is untrusted input, so these routes apply the
same guard `auth.service._check_poa_file` applies to a power of attorney: the
`media_files` row must exist, be active, and be the caller's OWN upload.
"""

import uuid


async def _upload(client) -> str:
    """A real file through the real route, so `media_files.uploaded_by` is the
    caller — which is exactly what the attach guard reads."""
    result = await client.post(
        "/api/v1/files",
        files={"file": ("proof.pdf", b"%PDF-1.4 test", "application/pdf")},
    )
    assert result.status_code == 201, result.text
    return result.json()["id"]


async def test_a_document_is_attached_listed_and_detached(
    db, applicant_client, draft_ready_for_submission, doc_type_item_id
) -> None:
    """Both actions are state-changing, so both audit under this module's own
    constants (ruling 17: `"<object>.<verb>"`, never a literal at the call
    site). The detach entry carries the removed row in `old_value` — after the
    DELETE it is the only record that the attachment ever existed."""
    from sqlalchemy import select

    from app.modules.applications.service import (
        APPLICATION_DOCUMENT_ATTACH,
        APPLICATION_DOCUMENT_DETACH,
    )
    from app.modules.audit.models import AuditLog

    app_id = draft_ready_for_submission
    file_id = await _upload(applicant_client)

    attached = await applicant_client.post(
        f"/api/v1/applications/{app_id}/documents",
        json={"doc_type_item_id": str(doc_type_item_id), "file_id": file_id},
    )
    assert attached.status_code == 201, attached.text
    document_id = attached.json()["id"]

    card = (await applicant_client.get(f"/api/v1/applications/{app_id}")).json()
    assert [d["id"] for d in card["documents"]] == [document_id]

    removed = await applicant_client.delete(
        f"/api/v1/applications/{app_id}/documents/{document_id}"
    )
    assert removed.status_code == 204

    card = (await applicant_client.get(f"/api/v1/applications/{app_id}")).json()
    assert card["documents"] == []

    entries = {
        entry.action: entry
        for entry in (
            await db.execute(select(AuditLog).where(AuditLog.object_id == uuid.UUID(document_id)))
        )
        .scalars()
        .all()
    }
    assert set(entries) == {APPLICATION_DOCUMENT_ATTACH, APPLICATION_DOCUMENT_DETACH}
    assert entries[APPLICATION_DOCUMENT_ATTACH].object_type == "application_document"
    assert entries[APPLICATION_DOCUMENT_ATTACH].new_value["file_id"] == file_id
    assert entries[APPLICATION_DOCUMENT_DETACH].old_value["file_id"] == file_id


async def test_a_file_the_caller_does_not_own_cannot_be_attached(
    applicant_client, other_applicant_client, draft_ready_for_submission, doc_type_item_id
) -> None:
    """The stranger's own upload is a real, active `media_files` row — only
    `uploaded_by` tells it apart, which is why an existence check alone would
    let an applicant hang somebody else's document on their application."""
    stranger_file = await _upload(other_applicant_client)

    refused = await applicant_client.post(
        f"/api/v1/applications/{draft_ready_for_submission}/documents",
        json={"doc_type_item_id": str(doc_type_item_id), "file_id": stranger_file},
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["reason"] == "document_file_not_owned"


async def test_an_unknown_doc_type_is_refused_before_the_insert(
    applicant_client, draft_ready_for_submission
) -> None:
    """An unknown FK reaching `flush()` is an `IntegrityError` with no handler —
    a 500 for what is only ever a typo (lesson)."""
    file_id = await _upload(applicant_client)
    refused = await applicant_client.post(
        f"/api/v1/applications/{draft_ready_for_submission}/documents",
        json={"doc_type_item_id": str(uuid.uuid4()), "file_id": file_id},
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["reason"] == "unknown_doc_type"


async def test_a_stranger_can_neither_attach_nor_detach(
    applicant_client, other_applicant_client, draft_ready_for_submission, doc_type_item_id
) -> None:
    """Ownership leaks are 404, never 403."""
    app_id = draft_ready_for_submission
    file_id = await _upload(applicant_client)
    attached = await applicant_client.post(
        f"/api/v1/applications/{app_id}/documents",
        json={"doc_type_item_id": str(doc_type_item_id), "file_id": file_id},
    )
    document_id = attached.json()["id"]

    stranger_file = await _upload(other_applicant_client)
    refused = await other_applicant_client.post(
        f"/api/v1/applications/{app_id}/documents",
        json={"doc_type_item_id": str(doc_type_item_id), "file_id": stranger_file},
    )
    assert refused.status_code == 404

    refused = await other_applicant_client.delete(
        f"/api/v1/applications/{app_id}/documents/{document_id}"
    )
    assert refused.status_code == 404


async def test_the_owner_may_attach_and_detach_on_a_returned_application(
    db, applicant_client, submitted_application, doc_type_item_id
) -> None:
    """`service._EDITABLE_STATUSES` deliberately holds `{DRAFT, RETURNED}`
    (3.9b task 1 review, Important finding): a returned application is
    correctable again, which includes its attachments, not just its fields —
    PATCH and the document routes share ONE definition of "still editable"
    (`_own_draft_for_update`) for exactly this reason. Do NOT narrow it back
    to DRAFT-only.

    Built by injecting the status directly, the same way
    `test_assignment.py::test_a_resubmission_does_not_re_fire_auto_assignment`
    does — Task 3's `/return` route does not exist yet and will produce
    RETURNED for real once it ships. The assertions read the card back after
    each step, not merely the status code, so a route that silently no-ops
    on a RETURNED application would not pass here.
    """
    import uuid as _uuid

    from sqlalchemy import update

    from app.modules.applications.models import Application

    await db.execute(
        update(Application)
        .where(Application.id == _uuid.UUID(submitted_application))
        .values(status="RETURNED")
    )
    await db.commit()

    app_id = submitted_application
    file_id = await _upload(applicant_client)
    attached = await applicant_client.post(
        f"/api/v1/applications/{app_id}/documents",
        json={"doc_type_item_id": str(doc_type_item_id), "file_id": file_id},
    )
    assert attached.status_code == 201, attached.text
    document_id = attached.json()["id"]

    card = (await applicant_client.get(f"/api/v1/applications/{app_id}")).json()
    assert [d["id"] for d in card["documents"]] == [document_id]

    detached = await applicant_client.delete(
        f"/api/v1/applications/{app_id}/documents/{document_id}"
    )
    assert detached.status_code == 204

    card = (await applicant_client.get(f"/api/v1/applications/{app_id}")).json()
    assert card["documents"] == []


async def test_a_stranger_still_cannot_touch_a_returned_applications_documents(
    db, applicant_client, other_applicant_client, submitted_application, doc_type_item_id
) -> None:
    """RETURNED becoming editable again (task 1 review finding) must not also
    make it readable/writable by anyone but its owner — the SAME 404 a
    stranger already gets against a DRAFT (`test_a_stranger_can_neither_
    attach_nor_detach` above)."""
    import uuid as _uuid

    from sqlalchemy import update

    from app.modules.applications.models import Application

    await db.execute(
        update(Application)
        .where(Application.id == _uuid.UUID(submitted_application))
        .values(status="RETURNED")
    )
    await db.commit()

    app_id = submitted_application
    file_id = await _upload(applicant_client)
    attached = await applicant_client.post(
        f"/api/v1/applications/{app_id}/documents",
        json={"doc_type_item_id": str(doc_type_item_id), "file_id": file_id},
    )
    assert attached.status_code == 201, attached.text
    document_id = attached.json()["id"]

    stranger_file = await _upload(other_applicant_client)
    refused = await other_applicant_client.post(
        f"/api/v1/applications/{app_id}/documents",
        json={"doc_type_item_id": str(doc_type_item_id), "file_id": stranger_file},
    )
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "ERR-SYS-003"

    refused = await other_applicant_client.delete(
        f"/api/v1/applications/{app_id}/documents/{document_id}"
    )
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_document_of_another_application_is_not_detachable_through_this_one(
    applicant_client, draft_ready_for_submission, doc_type_item_id
) -> None:
    """The path names both ids and both are checked: a document id that belongs
    to a different application is a 404, not a silent cross-application
    delete."""
    app_id = draft_ready_for_submission
    file_id = await _upload(applicant_client)
    attached = await applicant_client.post(
        f"/api/v1/applications/{app_id}/documents",
        json={"doc_type_item_id": str(doc_type_item_id), "file_id": file_id},
    )
    document_id = attached.json()["id"]

    other = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    other_id = other.json()["id"]

    refused = await applicant_client.delete(
        f"/api/v1/applications/{other_id}/documents/{document_id}"
    )
    assert refused.status_code == 404


async def test_the_benefit_proof_doc_type_is_seeded_and_active(db) -> None:
    """Migration `0024`'s row, and the only thing holding it to the constant
    the code looks it up by.

    `service._benefit_doc_type` searches `doc_types` for exactly
    `BENEFIT_DOC_TYPE_CODE` and refuses every benefit claim when it finds
    nothing (ruling 10а, fail-closed). The migration writes the code as a
    LITERAL on purpose — a migration is a frozen historical statement and must
    not change meaning when a constant is renamed — so a rename would leave the
    guard looking for a code nothing seeds, refusing every benefit claim in
    production with a message about configuration. This assertion is what turns
    that into a red test instead.
    """
    from sqlalchemy import select

    from app.modules.admin.models import Classifier, ClassifierItem
    from app.modules.applications.service import BENEFIT_DOC_TYPE_CODE

    item = await db.scalar(
        select(ClassifierItem)
        .join(Classifier, Classifier.id == ClassifierItem.classifier_id)
        .where(
            Classifier.code == "doc_types",
            ClassifierItem.code == BENEFIT_DOC_TYPE_CODE,
            ClassifierItem.status == "active",
        )
    )
    assert item is not None, (
        "migration 0024 seeds this row; without it every benefit claim is refused "
        "as 'benefit_doc_type_not_configured' — which must mean the Agency has not "
        "answered, never that we forgot a row"
    )
    # Bilingual, like every other seeded classifier item: `uz_cyrl` is the one
    # key design/02 guarantees and `decision.FALLBACK_LANGUAGE` reads.
    assert "uz_cyrl" in item.name
