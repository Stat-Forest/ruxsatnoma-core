"""Stage 16 — downloading the two printouts."""

import uuid

from sqlalchemy import select, text

from app.modules.applications.models import (
    Application,
    ApplicationPrintout,
    ApplicationRejectionGround,
)
from tests.modules.applications.test_decision import _decide, _ground, _reject_body


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


async def test_an_unrenderable_fact_refuses_the_rejection_before_the_signature(
    db, executor_head_client, application_in_review, rejection_reason_item
) -> None:
    """Stage 16 fix wave F1 — INVERTED from this test's own prior behaviour
    ("reject 200, download 422"): `decision.reject` now calls
    `pdf.assert_renderable` over every ground's text BEFORE `_sign_decision`
    spends the head's ERI, so an emoji the bundled DejaVu Serif face cannot
    draw is refused at REJECT time. Snapshots are immutable once recorded, so
    the old behaviour signed and froze a rejection notice that could never be
    rendered — permanently."""
    result = await _decide(
        executor_head_client,
        application_in_review,
        "reject",
        **_reject_body(rejection_reason_item, _ground(rejection_reason_item, fact="Ali 🙂")),
    )
    assert result.status_code == 422, result.text
    body = result.json()
    assert body["error"]["code"] == "ERR-VAL-001"
    assert body["error"]["details"]["reason"] == "unrenderable_characters"
    assert "grounds.0.fact" in body["error"]["details"]["fields"]

    timeline = (
        await executor_head_client.get(f"/api/v1/applications/{application_in_review}/timeline")
    ).json()
    assert timeline["signatures"] == [], "no signature may be spent on an unrenderable ground"

    application = await db.get(
        Application, uuid.UUID(application_in_review), populate_existing=True
    )
    assert application.status == "IN_REVIEW"

    grounds = (
        (
            await db.execute(
                select(ApplicationRejectionGround).where(
                    ApplicationRejectionGround.application_id == uuid.UUID(application_in_review)
                )
            )
        )
        .scalars()
        .all()
    )
    assert grounds == []

    printout = (
        await db.execute(
            select(ApplicationPrintout).where(
                ApplicationPrintout.application_id == uuid.UUID(application_in_review),
                ApplicationPrintout.kind == "rejection_notice",
            )
        )
    ).scalar_one_or_none()
    assert printout is None


async def test_a_bom_and_zero_width_space_are_stripped_from_a_rejection_ground(
    db, executor_head_client, application_in_review, rejection_reason_item
) -> None:
    """F1.2: `strip_invisible` runs on every ground field BEFORE pydantic's
    own `min_length=1` — a BOM or zero-width space pasted from Word must not
    silently ride into the append-only `application_rejection_grounds` row."""
    dirty_fact = "﻿Birinchi​ holat"
    result = await _decide(
        executor_head_client,
        application_in_review,
        "reject",
        **_reject_body(rejection_reason_item, _ground(rejection_reason_item, fact=dirty_fact)),
    )
    assert result.status_code == 200, result.text
    row = (
        await db.execute(
            select(ApplicationRejectionGround).where(
                ApplicationRejectionGround.application_id == uuid.UUID(application_in_review)
            )
        )
    ).scalar_one()
    assert "﻿" not in row.fact
    assert "​" not in row.fact
    assert row.fact == "Birinchi holat"


async def test_an_archived_stored_file_refuses_the_download(
    db, applicant_client, submitted_application
) -> None:
    """The same existence-plus-active guard every other file read in this
    codebase applies (`core/files.py::get_readable`, `applications/service.py`'s
    own `_own_document_file`/`_assert_check_doc_active`, `permits/service.py`'s
    `pdf_bytes`) — a stored `file_id` pointing at an archived `media_files` row
    is refused exactly like a missing one, never served from storage anyway."""
    first = await applicant_client.get(f"/api/v1/applications/{submitted_application}/letter.pdf")
    assert first.status_code == 200, first.text
    row = (
        await db.execute(
            select(ApplicationPrintout).where(
                ApplicationPrintout.application_id == uuid.UUID(submitted_application)
            )
        )
    ).scalar_one()
    await db.refresh(row)
    assert row.file_id is not None
    await db.execute(
        text("UPDATE media_files SET status = 'archived' WHERE id = :id"), {"id": row.file_id}
    )
    await db.commit()
    second = await applicant_client.get(f"/api/v1/applications/{submitted_application}/letter.pdf")
    assert second.status_code == 404
