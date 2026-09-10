"""`beekeepers.service.match_certificate` — the seam `applications` (wave 2)
calls. Direct service-level calls against real committed rows: the function
itself IS the seam under test, so its setup is a plain ORM row, not a second
layer of indirection through the HTTP API (`test_router.py` already walks
that layer for the CRUD routes)."""

import uuid

from app.modules.beekeepers import repo
from app.modules.beekeepers.models import Beekeeper
from app.modules.beekeepers.service import MatchResult, match_certificate
from tests.modules.auth.test_sessions import make_user


def unique_pinfl() -> str:
    return f"4{uuid.uuid4().int % 10**13:013d}"


async def make_beekeeper(
    db,
    *,
    certificate_no: str,
    pinfl: str,
    stir: str | None = None,
    status: str = "active",
    removed_reason: str | None = None,
) -> Beekeeper:
    actor = await make_user(db, role_code="executor_staff")
    row = Beekeeper(
        certificate_no=certificate_no,
        pinfl=pinfl,
        passport_series="AB",
        passport_number="1234567",
        stir=stir,
        full_name="Test Beekeeper",
        status=status,
        removed_reason=removed_reason,
        created_by=actor.id,
        updated_by=actor.id,
    )
    await repo.add(db, row)
    await db.commit()
    return row


async def test_matched_by_pinfl(db):
    certificate_no = f"AUZ-{uuid.uuid4().hex[:10]}"
    pinfl = unique_pinfl()
    row = await make_beekeeper(db, certificate_no=certificate_no, pinfl=pinfl)

    result = await match_certificate(db, certificate_no=certificate_no, pinfl=pinfl, stir=None)

    assert result == MatchResult(status="matched", beekeeper_id=row.id)


async def test_matched_by_stir_for_a_legal_entity(db):
    certificate_no = f"AUZ-{uuid.uuid4().hex[:10]}"
    stir = f"{uuid.uuid4().int % 10**9:09d}"
    row = await make_beekeeper(db, certificate_no=certificate_no, pinfl=unique_pinfl(), stir=stir)

    result = await match_certificate(db, certificate_no=certificate_no, pinfl=None, stir=stir)

    assert result == MatchResult(status="matched", beekeeper_id=row.id)


async def test_unknown_when_no_active_row_has_that_number(db):
    result = await match_certificate(
        db, certificate_no=f"NOPE-{uuid.uuid4().hex}", pinfl=unique_pinfl(), stir=None
    )
    assert result == MatchResult(status="unknown", beekeeper_id=None)


async def test_not_yours_when_the_identity_does_not_match(db):
    certificate_no = f"AUZ-{uuid.uuid4().hex[:10]}"
    await make_beekeeper(db, certificate_no=certificate_no, pinfl=unique_pinfl())

    result = await match_certificate(
        db, certificate_no=certificate_no, pinfl=unique_pinfl(), stir=None
    )

    assert result == MatchResult(status="not_yours", beekeeper_id=None)


async def test_a_removed_row_reads_as_unknown_not_as_a_stale_match(db):
    """The one-line rule the plan spells out explicitly: a removed member's
    number must never re-match, not even for the identity that originally
    owned it — `repo.get_active_by_certificate_no` filters `status='active'`
    only."""
    certificate_no = f"AUZ-{uuid.uuid4().hex[:10]}"
    pinfl = unique_pinfl()
    await make_beekeeper(
        db,
        certificate_no=certificate_no,
        pinfl=pinfl,
        status="removed",
        removed_reason="left the union",
    )

    result = await match_certificate(db, certificate_no=certificate_no, pinfl=pinfl, stir=None)

    assert result == MatchResult(status="unknown", beekeeper_id=None)


async def test_case_and_whitespace_folding(db):
    """The register holds the number as the registrar typed it; the
    applicant's own typed number may differ only in case or surrounding
    whitespace and must still match."""
    certificate_no = f"AUZ-{uuid.uuid4().hex[:10].upper()}"
    pinfl = unique_pinfl()
    row = await make_beekeeper(db, certificate_no=certificate_no, pinfl=pinfl)

    result = await match_certificate(
        db, certificate_no=f"  {certificate_no.lower()}  ", pinfl=pinfl, stir=None
    )

    assert result == MatchResult(status="matched", beekeeper_id=row.id)


async def test_not_yours_when_neither_pinfl_nor_stir_is_given(db):
    """A defensive default, not a real caller shape: `applications` always
    passes one or the other by `on_behalf`. The number IS in the register,
    so the honest answer is "not provably yours" — never `unknown`, which
    would tell the applicant to check a number that is perfectly real, and
    never an exception (the plan's own words: "no exceptions for business
    outcomes")."""
    certificate_no = f"AUZ-{uuid.uuid4().hex[:10]}"
    await make_beekeeper(db, certificate_no=certificate_no, pinfl=unique_pinfl())

    result = await match_certificate(db, certificate_no=certificate_no, pinfl=None, stir=None)

    assert result == MatchResult(status="not_yours", beekeeper_id=None)
