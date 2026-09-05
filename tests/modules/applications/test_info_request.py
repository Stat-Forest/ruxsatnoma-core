"""Request for information and the SLA pause it opens (plan 03.9b task 4).

`request-info` (`applications.review`, zone) opens an `info_requests` row and
moves SUBMITTED/IN_REVIEW -> PENDING_INFO. `respond-info` (the owner) closes
the newest open one, attaches its files as `application_documents`, and shifts
`sla_deadline_at` forward by exactly the length of the pause (ruling 8) —
proved below against a controlled clock, not merely by a status change.
"""

from datetime import datetime, timedelta


async def test_the_clock_stops_while_the_applicant_is_being_waited_on(
    db, hodim_client, applicant_client, application_in_review
) -> None:
    before = (await hodim_client.get(f"/api/v1/applications/{application_in_review}")).json()

    asked = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/request-info",
        json={"message": "Прикрепите ветеринарную справку"},
    )
    assert asked.status_code == 200, asked.text
    assert asked.json()["status"] == "PENDING_INFO"

    paused = (await hodim_client.get(f"/api/v1/applications/{application_in_review}")).json()
    assert paused["sla_overdue"] is False
    assert paused["sla_deadline_at"] == before["sla_deadline_at"], (
        "the deadline shifts when the pause CLOSES, not when it opens"
    )


async def test_answering_shifts_the_deadline_by_the_pause(
    db, hodim_client, applicant_client, application_in_review, frozen_clock
) -> None:
    before = (await hodim_client.get(f"/api/v1/applications/{application_in_review}")).json()

    await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/request-info",
        json={"message": "Прикрепите справку"},
    )
    frozen_clock.advance(timedelta(days=3))
    answered = await applicant_client.post(
        f"/api/v1/applications/{application_in_review}/respond-info",
        json={"text": "Прикрепил", "file_ids": []},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["status"] == "IN_REVIEW"

    after = (await hodim_client.get(f"/api/v1/applications/{application_in_review}")).json()
    assert (
        datetime.fromisoformat(after["sla_deadline_at"])
        - datetime.fromisoformat(before["sla_deadline_at"])
    ) == timedelta(days=3)


async def test_a_second_open_request_is_refused(hodim_client, application_in_review) -> None:
    """Two open pauses make the arithmetic ambiguous — refuse rather than guess.

    Final whole-branch review, MINOR: asserting only the status code passes
    even with the `info_request_already_open` guard deleted outright, because
    `_assert_transition` supplies an identical 409 (`bad_transition`) once the
    application is already PENDING_INFO — proved by the reviewer disabling the
    guard and watching all five tests in this file stay green. The `reason`
    is what tells the two 409s apart, and only asserting it pins the guard
    this test is actually named for."""
    await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/request-info",
        json={"message": "первый"},
    )
    second = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/request-info",
        json={"message": "второй"},
    )
    assert second.status_code == 409
    assert second.json()["error"]["details"]["reason"] == "info_request_already_open"


async def test_the_response_files_become_application_documents(
    applicant_client, hodim_client, application_in_review, vet_certificate_file
) -> None:
    await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/request-info",
        json={"message": "справку"},
    )
    await applicant_client.post(
        f"/api/v1/applications/{application_in_review}/respond-info",
        json={"text": "вот", "file_ids": [str(vet_certificate_file.id)]},
    )
    card = (await hodim_client.get(f"/api/v1/applications/{application_in_review}")).json()
    assert any(d["file_id"] == str(vet_certificate_file.id) for d in card["documents"])


async def test_an_info_response_document_is_not_accepted_as_benefit_proof(
    hodim_client,
    applicant_client,
    application_in_review,
    rj_01_return_reason,
    benefit_category_item_id,
    vet_certificate_file,
) -> None:
    """Fix round 1's Critical: the first draft filed every `respond-info`
    attachment under "the first ACTIVE `doc_types` item", which resolved to
    `benefit_proof` in every migrated database and was a REACHABLE BYPASS of
    `_assert_benefit_documents`'s fail-closed guard — submit, `request-info`,
    `respond-info` with an unrelated file, `return`, PATCH in a benefit claim,
    resubmit, and the stray info-response document waved the claim through
    with no real proof.

    Migration `0025` now reserves a dedicated `info_response` code and
    `service._info_response_doc_type` fails closed rather than falling back
    to anything, so the exact same sequence must still be REFUSED — at the
    same gate (`ERR-APP-003`/`benefit_claim_needs_a_document`)
    `test_submit.py::test_a_benefit_claim_needs_a_document_of_the_benefit_
    type_and_no_other` already proves for a document attached the ordinary
    way. This is the negative nothing asserted before: an info-response
    document is NOT a document of the benefit type, however it got attached.
    """
    from tests.modules.applications.test_submit import _submit

    await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/request-info",
        json={"message": "справку"},
    )
    answered = await applicant_client.post(
        f"/api/v1/applications/{application_in_review}/respond-info",
        json={"text": "вот", "file_ids": [str(vet_certificate_file.id)]},
    )
    assert answered.status_code == 200, answered.text

    returned = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/return",
        json={
            "reason_item_id": str(rj_01_return_reason.id),
            "fields_to_fix": {"benefit_category_item_id": "льгота не подтверждена документом"},
            "legal_basis": "ВМҚ 290",
        },
    )
    assert returned.status_code == 200, returned.text

    patched = await applicant_client.patch(
        f"/api/v1/applications/{application_in_review}",
        json={"benefit_category_item_id": str(benefit_category_item_id)},
    )
    assert patched.status_code == 200, "a RETURNED application must be editable again"

    resubmitted = await _submit(applicant_client, application_in_review)
    assert resubmitted.status_code == 422, resubmitted.text
    error = resubmitted.json()["error"]
    assert error["code"] == "ERR-APP-003"
    assert error["details"]["reason"] == "benefit_claim_needs_a_document", (
        "the info-response document must not count as proof of the benefit claim"
    )


async def test_the_timeline_lists_the_pause_open_and_then_closed(
    hodim_client, applicant_client, application_in_review
) -> None:
    """Final whole-branch review, IMPORTANT: the pause is the one event on
    this branch that silently moves a legally-consequential deadline
    (`sla_deadline_at`), and it used to appear NOWHERE in the only audit view
    — `GET /timeline`'s `info_requests` stayed `[]` by contract even after
    task 4 started writing the table. An inspector reading the timeline while
    the pause was open would have seen no explanation at all for a stalled
    file; one reading it afterwards would have seen no explanation for why
    the deadline had moved."""
    opened = (
        await hodim_client.get(f"/api/v1/applications/{application_in_review}/timeline")
    ).json()
    assert opened["info_requests"] == []

    await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/request-info",
        json={"message": "Уточните состав стада"},
    )

    while_open = (
        await applicant_client.get(f"/api/v1/applications/{application_in_review}/timeline")
    ).json()
    assert len(while_open["info_requests"]) == 1
    pending = while_open["info_requests"][0]
    assert pending["message"] == "Уточните состав стада"
    assert pending["responded_at"] is None, "still open — nothing has answered it yet"

    await applicant_client.post(
        f"/api/v1/applications/{application_in_review}/respond-info",
        json={"text": "40 голов", "file_ids": []},
    )

    after_close = (
        await hodim_client.get(f"/api/v1/applications/{application_in_review}/timeline")
    ).json()
    assert len(after_close["info_requests"]) == 1, "the same row, closed, not a second one"
    closed = after_close["info_requests"][0]
    assert closed["id"] == pending["id"]
    assert closed["response_text"] == "40 голов"
    assert closed["responded_at"] is not None
