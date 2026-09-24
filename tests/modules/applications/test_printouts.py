"""Stage 16 — application printouts and rejection grounds: the DB-level
invariants migration 0064 enforces (append-only grounds, a frozen printout
whose first render may be stored exactly once, no delete/truncate on either
table). tz/05's own append-only idiom, extended to this stage's two tables."""

import re
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.schemas import LOCALES
from app.modules.applications import printout_labels as labels
from app.modules.applications.models import ApplicationPrintout
from tests.modules.applications.test_decision import _decide, _reject_body


async def _insert_letter(db: AsyncSession, application_id: str) -> uuid.UUID:
    row_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO application_printouts "
            "(id, application_id, kind, submission_id, language, snapshot) "
            "VALUES (:id, :app, 'letter', :sub, 'uz_latn', '{\"a\": 1}')"
        ),
        {"id": row_id, "app": uuid.UUID(application_id), "sub": uuid.uuid4()},
    )
    return row_id


async def test_a_printout_snapshot_cannot_change(
    db: AsyncSession, submitted_application: str
) -> None:
    row_id = await _insert_letter(db, submitted_application)
    with pytest.raises(DBAPIError, match="frozen"):
        async with db.begin_nested():
            await db.execute(
                text("UPDATE application_printouts SET snapshot = '{}' WHERE id = :id"),
                {"id": row_id},
            )


async def test_the_first_render_may_be_stored_once(
    db: AsyncSession, submitted_application: str, vet_certificate_file
) -> None:
    row_id = await _insert_letter(db, submitted_application)
    await db.execute(
        text("UPDATE application_printouts SET file_id = :f, sha256 = 'x' WHERE id = :id"),
        {"f": vet_certificate_file.id, "id": row_id},
    )
    with pytest.raises(DBAPIError, match="frozen"):
        async with db.begin_nested():
            await db.execute(
                text("UPDATE application_printouts SET sha256 = 'y' WHERE id = :id"), {"id": row_id}
            )


async def test_a_printout_is_never_deleted(db: AsyncSession, submitted_application: str) -> None:
    row_id = await _insert_letter(db, submitted_application)
    with pytest.raises(DBAPIError, match="never deleted"):
        async with db.begin_nested():
            await db.execute(
                text("DELETE FROM application_printouts WHERE id = :id"), {"id": row_id}
            )


async def test_rejection_grounds_are_append_only(
    db: AsyncSession, submitted_application: str, rejection_reason_item, staff_user
) -> None:
    await db.execute(
        text(
            "INSERT INTO application_rejection_grounds "
            "(id, application_id, position, reason_item_id, fact, legal_document, legal_clause, "
            " evidence, remedy, created_by) "
            "VALUES (:id, :app, 1, :r, 'f', 'd', 'c', 'e', 'm', :u)"
        ),
        {
            "id": uuid.uuid4(),
            "app": uuid.UUID(submitted_application),
            "r": rejection_reason_item.id,
            "u": staff_user.id,
        },
    )
    with pytest.raises(DBAPIError, match="append-only"):
        async with db.begin_nested():
            await db.execute(text("UPDATE application_rejection_grounds SET fact = 'x'"))


# --- Stage 16 (rulings R5-R7): the rejection notice's frozen snapshot --------


def test_every_wording_table_covers_all_five_languages() -> None:
    for name in dir(labels):
        table = getattr(labels, name)
        if isinstance(table, dict) and name.isupper():
            assert set(table) == set(LOCALES), name
    for table in (labels.LETTER_LABELS, labels.NOTICE_LABELS):
        keys = {lang: set(t) for lang, t in table.items()}
        assert len({frozenset(k) for k in keys.values()}) == 1, "every language has the same keys"


async def test_a_rejection_freezes_its_notice_in_the_recipients_language(
    db, applicant_user, executor_head_client, application_in_review, rejection_reason_item
) -> None:
    applicant_user.language = "ru"
    await db.commit()
    result = await _decide(
        executor_head_client, application_in_review, "reject", **_reject_body(rejection_reason_item)
    )
    assert result.status_code == 200, result.text
    row = (
        await db.execute(
            select(ApplicationPrintout).where(
                ApplicationPrintout.application_id == uuid.UUID(application_in_review),
                ApplicationPrintout.kind == "rejection_notice",
            )
        )
    ).scalar_one()
    assert row.language == "ru"
    assert re.fullmatch(r"RD-\d{4}-\d{6}", row.number)
    snap = row.snapshot
    assert snap["grounds"][0]["code"] == "R01"
    assert snap["grounds"][0]["name"] == "Сведения неполны или противоречивы"
    assert snap["grounds"][0]["legal"] == "VMQ 689, 12-band"
    assert snap["reapply_text"] == "Qayta ariza bering"
    assert all(isinstance(v, (str, list)) for v in snap.values()), "a snapshot holds strings only"


async def test_rejection_defaults_follow_the_recipients_language(
    db, applicant_user, executor_head_client, application_in_review
) -> None:
    applicant_user.language = "uz_cyrl"
    await db.commit()
    result = await executor_head_client.get(
        f"/api/v1/applications/{application_in_review}/rejection-defaults"
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["language"] == "uz_cyrl"
    assert body["appeal_text"].startswith("Ушбу қарорга")


async def test_rejection_defaults_need_the_decide_permission(
    hodim_client, application_in_review
) -> None:
    result = await hodim_client.get(
        f"/api/v1/applications/{application_in_review}/rejection-defaults"
    )
    assert result.status_code == 403
