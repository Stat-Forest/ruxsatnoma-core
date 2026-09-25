"""`applications.service.owned_application_ids` — the seam `payments` reads a
citizen's "all of mine" invoices and refunds through (stage 11, ruling R2).
Ownership only: exactly the caller's own row, individual or legal (decision
#226, R4), every status (a just-filed SUBMITTED one included — stage 12 has
no DRAFT), and never a staff zone."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications import service
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, User
from tests.modules.applications.conftest import unique_pinfl
from tests.modules.auth.test_sessions import make_user


async def _application(db: AsyncSession, applicant: Applicant, *, status: str) -> Application:
    """`status` is assigned directly, never through `applications.service`:
    `owned_application_ids` reads ownership only and never looks at
    `status` itself, so a real transition adds nothing this file's own
    tests would notice."""
    submitter = applicant.owner_user_id
    if submitter is None:
        submitter = (await make_user(db, role_code="applicant", pinfl=unique_pinfl())).id
    row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=submitter,
        on_behalf="self" if applicant.kind == "individual" else "legal",
        channel="portal",
        status=status,
    )
    db.add(row)
    await db.flush()
    return row


async def test_the_owner_gets_every_status_of_their_own_and_nothing_of_a_strangers(
    db: AsyncSession, applicant: Applicant, legal_applicant: Applicant
) -> None:
    submitted = await _application(db, applicant, status="SUBMITTED")
    approved = await _application(db, applicant, status="APPROVED")
    strangers = await _application(db, legal_applicant, status="APPROVED")
    owner = await db.get(User, applicant.owner_user_id)
    assert owner is not None

    ids = await service.owned_application_ids(db, owner)

    assert {submitted.id, approved.id} <= set(ids)
    assert strangers.id not in ids


async def test_a_legal_entitys_own_account_gets_its_own_applications(
    db: AsyncSession, legal_applicant: Applicant
) -> None:
    """Decision #226 (R4): a legal applicant's own account (`owner_user_id`)
    sees its applications the same way an individual does — no more separate
    representative identity to grant or lapse."""
    owner = await db.get(User, legal_applicant.owner_user_id)
    assert owner is not None
    theirs = await _application(db, legal_applicant, status="INVOICED")

    assert theirs.id in await service.owned_application_ids(db, owner)


async def test_a_staff_user_with_no_applicant_row_gets_an_empty_list(db: AsyncSession) -> None:
    staff = await make_user(db, role_code="accountant")
    assert await service.owned_application_ids(db, staff) == []
