"""Stage 16 — application printouts and rejection grounds: the DB-level
invariants migration 0064 enforces (append-only grounds, a frozen printout
whose first render may be stored exactly once, no delete/truncate on either
table). tz/05's own append-only idiom, extended to this stage's two tables."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


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
