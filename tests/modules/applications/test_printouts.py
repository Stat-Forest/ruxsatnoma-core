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
from app.modules.applications import printouts
from app.modules.applications.models import ApplicationPrintout
from app.modules.auth.models import User
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


async def _notice(db: AsyncSession, application_id: str) -> ApplicationPrintout:
    return (
        await db.execute(
            select(ApplicationPrintout).where(
                ApplicationPrintout.application_id == uuid.UUID(application_id),
                ApplicationPrintout.kind == "rejection_notice",
            )
        )
    ).scalar_one()


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
    row = await _notice(db, application_in_review)
    assert row.language == "ru"
    assert row.number is not None and re.fullmatch(r"RD-\d{4}-\d{6}", row.number)
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


# --- Stage 16 fix wave F2/F3/F5: address language, moderator, territory -----


async def test_the_addressee_address_follows_the_notices_own_language(
    db,
    applicant,
    applicant_user,
    executor_head_client,
    application_in_review,
    rejection_reason_item,
) -> None:
    """F2: `printouts._applicant_address` used to read region/district names
    with a hardcoded `FALLBACK_LANGUAGE` ("uz_latn") no matter the document's
    own language — a uz_cyrl notice printed a Latin-script region name
    mid-Cyrillic sentence. It now takes `language` and reads with it; every
    region carries BOTH scripts (migration 0032's uz_latn backfill), so a
    uz_cyrl document must show the uz_cyrl name, not the transliteration."""
    region_id = (await db.execute(text("SELECT id FROM regions LIMIT 1"))).scalar_one()
    region_name = (
        await db.execute(text("SELECT name FROM regions WHERE id = :id"), {"id": region_id})
    ).scalar_one()
    applicant.region_id = region_id
    applicant_user.language = "uz_cyrl"
    await db.commit()

    result = await _decide(
        executor_head_client, application_in_review, "reject", **_reject_body(rejection_reason_item)
    )
    assert result.status_code == 200, result.text
    row = await _notice(db, application_in_review)
    assert row.language == "uz_cyrl"
    address = row.snapshot["addressee_address"]
    assert region_name["uz_cyrl"] in address
    assert region_name["uz_latn"] not in address


async def test_moderator_prints_the_signers_position_when_set(
    db, executor_head_client, application_in_review, rejection_reason_item
) -> None:
    """F3: the blank's «Модератор» line wants the signer's POSITION
    (`users.position`, nullable) — `record_rejection_notice` used to always
    print the localized ROLE name instead."""
    from tests.modules.applications.conftest import EXECUTOR_HEAD_PINFL

    signer = (await db.execute(select(User).where(User.pinfl == EXECUTOR_HEAD_PINFL))).scalar_one()
    signer.position = "Bosh oʻrmonchi"
    await db.commit()

    result = await _decide(
        executor_head_client, application_in_review, "reject", **_reject_body(rejection_reason_item)
    )
    assert result.status_code == 200, result.text
    row = await _notice(db, application_in_review)
    assert row.snapshot["moderator"].startswith("Bosh oʻrmonchi,")


async def test_moderator_falls_back_to_the_localized_role_name_with_no_position(
    db, executor_head_client, application_in_review, rejection_reason_item
) -> None:
    """The other branch of F3: a blank `users.position` (the common case
    today) keeps printing the localized role name, exactly as before this
    fix."""
    from tests.modules.applications.conftest import EXECUTOR_HEAD_PINFL

    signer = (await db.execute(select(User).where(User.pinfl == EXECUTOR_HEAD_PINFL))).scalar_one()
    signer.position = None
    await db.commit()

    result = await _decide(
        executor_head_client, application_in_review, "reject", **_reject_body(rejection_reason_item)
    )
    assert result.status_code == 200, result.text
    row = await _notice(db, application_in_review)
    assert not row.snapshot["moderator"].startswith("Bosh oʻrmonchi,")
    assert row.snapshot["moderator"].endswith(signer.full_name)


async def test_the_letter_names_only_the_known_territory_parts(
    db, leshoz, applicant_client, filing_ready_for_submission
) -> None:
    """F5: a leshoz with a region but no district (Burchmulla, decision #45's
    own sample) used to print "Тошкент вилояти / — / Бурчмулла ДЎХ" — built
    from the NON-EMPTY parts only now; `—` means the WHOLE line is unknown,
    never one field of three."""
    from tests.modules.applications.test_submit import _submit_with_button

    region_id = (await db.execute(text("SELECT id FROM regions LIMIT 1"))).scalar_one()
    leshoz.region_id = region_id
    leshoz.district_id = None
    await db.commit()

    result = await _submit_with_button(applicant_client, filing_ready_for_submission)
    assert result.status_code == 201, result.text
    row = await _letter(db, result.json()["id"])
    territory = row.snapshot["territory"]
    assert "—" not in territory
    assert territory.count(" / ") == 1


# --- Task B4 (rulings R6/R7/R11): the application letter's frozen snapshot ---


async def _letter(db, application_id: str) -> ApplicationPrintout:
    rows = (
        (
            await db.execute(
                select(ApplicationPrintout)
                .where(
                    ApplicationPrintout.application_id == uuid.UUID(application_id),
                    ApplicationPrintout.kind == "letter",
                )
                .order_by(ApplicationPrintout.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert rows, "a filed application has a letter"
    return rows[-1]


async def test_filing_freezes_a_letter(db, submitted_application: str) -> None:
    row = await _letter(db, submitted_application)
    snap = row.snapshot
    assert snap["number"].startswith("RX-")
    assert snap["plot"].startswith(("Kontur №", "Контур №", "Contour No."))
    assert snap["coordinates"] != ""
    assert snap["package_sha256"] and len(snap["package_sha256"]) == 64
    assert all(isinstance(v, str) for v in snap.values()), "a letter snapshot holds strings only"


async def test_an_uncovered_character_in_the_applicants_own_address_never_blocks_the_letter(
    db, applicant, applicant_client, filing_ready_for_submission
) -> None:
    """Stage 16 fix wave F1.4: `printouts.record_letter` runs every snapshot
    string through `pdf.renderable_text` — data nobody typed into THIS
    request (the applicant's own STORED address) must never make a signed,
    frozen letter unrenderable. Filing must never be blocked, and a document
    must always render (ruling R6)."""
    from tests.modules.applications.test_submit import _submit_with_button

    applicant.address = "Toshkent, 1-uy 🙂"
    await db.commit()
    result = await _submit_with_button(applicant_client, filing_ready_for_submission)
    assert result.status_code == 201, result.text
    application_id = result.json()["id"]

    download = await applicant_client.get(f"/api/v1/applications/{application_id}/letter.pdf")
    assert download.status_code == 200, download.text
    assert download.content.startswith(b"%PDF-")

    row = await _letter(db, application_id)
    address = row.snapshot["applicant_address"]
    assert "🙂" not in address
    assert "Toshkent, 1-uy" in address


async def test_the_letter_is_in_the_applicants_language(
    db, applicant_user, applicant_client, filing_ready_for_submission
) -> None:
    from tests.modules.applications.test_submit import _submit

    applicant_user.language = "kaa"
    await db.commit()
    result = await _submit(applicant_client, filing_ready_for_submission)
    assert result.status_code == 201, result.text
    row = await _letter(db, result.json()["id"])
    assert row.language == "kaa"
    assert row.snapshot["period"].endswith("ge shekem")


async def test_a_resubmission_freezes_a_second_letter(
    db, hodim_client, applicant_client, application_in_review, rj_01_return_reason
) -> None:
    """Ruling R7 — the same real path `test_return.py` walks: return, edit,
    re-sign, resubmit. Never a hand-set status."""
    from app.modules.applications import repo
    from tests.modules.applications.test_submit import _resubmit

    returned = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/return",
        json={
            "reason_item_id": str(rj_01_return_reason.id),
            "fields_to_fix": {"period_to": "srok"},
            "legal_basis": "VMQ 290",
        },
    )
    assert returned.status_code == 200, returned.text
    patched = await applicant_client.patch(
        f"/api/v1/applications/{application_in_review}", json={"period_to": "2027-08-31"}
    )
    assert patched.status_code == 200, patched.text
    again = await _resubmit(applicant_client, application_in_review)
    assert again.status_code == 200, again.text

    rows = (
        (
            await db.execute(
                select(ApplicationPrintout)
                .where(
                    ApplicationPrintout.application_id == uuid.UUID(application_in_review),
                    ApplicationPrintout.kind == "letter",
                )
                .order_by(ApplicationPrintout.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert rows[0].submission_id != rows[1].submission_id
    assert rows[1].snapshot["period"].count("31.08.2027") == 1, "the new letter carries the edit"
    latest = await repo.latest_printouts(db, uuid.UUID(application_in_review))
    assert [p.id for p in latest] == [rows[1].id]


# --- Task B5: rendering the two layouts, in every language -------------------

LETTER_SNAPSHOT = {
    "number": "RX-2026-000001",
    "date": "24.09.2026",
    "addressee": "Bo'stonliq o'rmon xo'jaligi rahbariga",
    "applicant_name": "Aliyev Vali",
    "applicant_address": "Toshkent viloyati, Boʻstonliq tumani",
    "contact": "+998901234567",
    "activity_name": "Chorva mollarini boqish",
    "territory": "Toshkent / Boʻstonliq / Burchmulla",
    "plot": "Kontur № 10517қ, maydon 12.5 ga",
    "coordinates": "41.600000, 70.100000",
    "period": "01.10.2026 dan 31.12.2026 gacha",
    "quantity": "Qoramol: 10",
    "purpose": "Chorva mollarini boqish",
    "attachments": "—",
    "signed_at": "24.09.2026 10:00",
    "signature": "OneID orqali tasdiqlangan, ID x",
    "package_sha256": "a" * 64,
    "created": "2026-09-24T10:00:00+05:00",
}
NOTICE_SNAPSHOT = {
    "number": "RD-2026-000001",
    "decided_at": "24.09.2026",
    "application_number": "RX-2026-000001",
    "addressee": "Алиев Валига",
    "addressee_address": "Тошкент вилояти, Бўстонлиқ тумани",
    "addressee_contact": "Шахсий кабинет",
    "reviewer": "Бурчмулла ўрмон хўжалиги / Каримов Анвар",
    "body": "Сизнинг 20.09.2026 куни берилган RX-2026-000001-сон аризангиз кўриб чиқилди.",
    "grounds": [
        {
            "index": str(i),
            "code": "R05",
            "name": "Ҳудуд ёки координатада устма-уст тушиш аниқланди",
            "fact": (
                "Uchastka RX-2026-000124 ruxsatnomasi bilan 2,3 ga kesishadi; Toǵay ń ı á ó ú ǵ"
            ),
            "legal": "VMQ 689 (19.08.2019), 12-band",
            "evidence": "GIS tekshiruvi 20.09.2026 — «ustma-ust tushish»",
            "remedy": "Boshqa kontur tanlang yoki muddatni oʻzgartiring",
        }
        for i in (1, 2, 3)
    ],
    "reapply_text": "Kamchiliklar bartaraf etilgandan soʻng qayta ariza berishingiz mumkin.",
    "appeal_text": "Если вы не согласны с решением, вы вправе обжаловать его в суд.",
    "moderator": "Rahbar, Каримов Анвар",
    "signature": "ERI, sertifikat № 7A1B2C",
    "signed_at": "24.09.2026 10:00",
    "created": "2026-09-24T10:00:00+05:00",
}


@pytest.mark.parametrize("language", ["uz_latn", "uz_cyrl", "ru", "kaa", "en"])
def test_both_documents_render_in_every_language(language: str) -> None:
    letter = printouts.render_pdf("letter", language, LETTER_SNAPSHOT)
    notice = printouts.render_pdf("rejection_notice", language, NOTICE_SNAPSHOT)
    assert letter.startswith(b"%PDF-") and notice.startswith(b"%PDF-")
    assert printouts.render_pdf("letter", language, LETTER_SNAPSHOT) == letter


def test_every_layout_placeholder_has_a_value() -> None:
    """The contract between layouts, labels and snapshots, pinned: `fill`
    refuses a gap, so rendering the fixed snapshots above in every language
    (the test before this) already proves it — this one names the layout
    keys so a new placeholder without a label fails HERE with its name."""
    import re

    for name, keys in (
        ("letter", set(LETTER_SNAPSHOT) | set(labels.LETTER_LABELS["uz_latn"]) | {"qr"}),
        ("notice", set(NOTICE_SNAPSHOT) | set(labels.NOTICE_LABELS["uz_latn"])),
        (
            "notice_ground",
            set(NOTICE_SNAPSHOT["grounds"][0]) | set(labels.NOTICE_LABELS["uz_latn"]),
        ),
    ):
        used = {m.strip() for m in re.findall(r"\{\{([^{}]*)\}\}", printouts._layout(name))}
        assert used <= keys, (name, used - keys)
