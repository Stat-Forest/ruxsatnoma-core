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
    applicant_client, draft_ready_for_submission, doc_type_item_id
) -> None:
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
