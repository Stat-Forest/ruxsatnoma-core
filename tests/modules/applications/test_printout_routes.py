"""Stage 16 — downloading the two printouts."""

import uuid

from sqlalchemy import select

from app.modules.applications.models import ApplicationPrintout
from tests.modules.applications.test_decision import _decide, _reject_body


async def test_the_applicant_downloads_their_letter(
    applicant_client, submitted_application
) -> None:
    result = await applicant_client.get(f"/api/v1/applications/{submitted_application}/letter.pdf")
    assert result.status_code == 200, result.text
    assert result.headers["content-type"] == "application/pdf"
    assert "attachment" in result.headers["content-disposition"]
    assert result.content.startswith(b"%PDF-")


async def test_the_first_download_stores_the_pdf_and_later_ones_return_it(
    db, applicant_client, submitted_application
) -> None:
    first = await applicant_client.get(f"/api/v1/applications/{submitted_application}/letter.pdf")
    second = await applicant_client.get(f"/api/v1/applications/{submitted_application}/letter.pdf")
    assert first.content == second.content
    row = (
        await db.execute(
            select(ApplicationPrintout).where(
                ApplicationPrintout.application_id == uuid.UUID(submitted_application)
            )
        )
    ).scalar_one()
    await db.refresh(row)
    assert row.file_id is not None and row.sha256 is not None


async def test_staff_in_zone_download_the_same_letter(
    applicant_client, hodim_client, submitted_application
) -> None:
    own = await applicant_client.get(f"/api/v1/applications/{submitted_application}/letter.pdf")
    staff = await hodim_client.get(f"/api/v1/applications/{submitted_application}/letter.pdf")
    assert staff.status_code == 200
    assert staff.content == own.content


async def test_a_stranger_is_told_404(other_applicant_client, submitted_application) -> None:
    result = await other_applicant_client.get(
        f"/api/v1/applications/{submitted_application}/letter.pdf"
    )
    assert result.status_code == 404


async def test_no_notice_before_a_rejection(applicant_client, submitted_application) -> None:
    result = await applicant_client.get(
        f"/api/v1/applications/{submitted_application}/rejection-notice.pdf"
    )
    assert result.status_code == 404


async def test_the_rejection_notice_downloads_after_a_rejection(
    applicant_client, executor_head_client, application_in_review, rejection_reason_item
) -> None:
    rejected = await _decide(
        executor_head_client, application_in_review, "reject", **_reject_body(rejection_reason_item)
    )
    assert rejected.status_code == 200, rejected.text
    result = await applicant_client.get(
        f"/api/v1/applications/{application_in_review}/rejection-notice.pdf"
    )
    assert result.status_code == 200, result.text
    assert "rad-etish-xati-RD-" in result.headers["content-disposition"]


async def test_the_card_lists_its_printouts(
    applicant_client, executor_head_client, application_in_review, rejection_reason_item
) -> None:
    card = (await applicant_client.get(f"/api/v1/applications/{application_in_review}")).json()
    assert [p["kind"] for p in card["printouts"]] == ["letter"]
    await _decide(
        executor_head_client, application_in_review, "reject", **_reject_body(rejection_reason_item)
    )
    card = (await applicant_client.get(f"/api/v1/applications/{application_in_review}")).json()
    assert [p["kind"] for p in card["printouts"]] == ["letter", "rejection_notice"]
