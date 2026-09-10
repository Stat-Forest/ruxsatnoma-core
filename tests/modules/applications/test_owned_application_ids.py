"""`applications.service.owned_application_ids` — the seam `payments` reads a
citizen's "all of mine" invoices and refunds through (stage 11, ruling R2).
Ownership only: the individual's own row plus every effectively represented
legal entity, every status including DRAFT, and never a staff zone."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import business_today
from app.modules.applications import service
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, Representation, User
from tests.modules.applications.conftest import unique_pinfl
from tests.modules.auth.test_sessions import make_user


async def _application(db: AsyncSession, applicant: Applicant, *, status: str) -> Application:
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
    draft = await _application(db, applicant, status="DRAFT")
    approved = await _application(db, applicant, status="APPROVED")
    strangers = await _application(db, legal_applicant, status="APPROVED")
    owner = await db.get(User, applicant.owner_user_id)
    assert owner is not None

    ids = await service.owned_application_ids(db, owner)

    assert {draft.id, approved.id} <= set(ids)
    assert strangers.id not in ids


async def test_a_representative_gets_the_legal_entitys_applications(
    db: AsyncSession, legal_applicant: Applicant
) -> None:
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    db.add(
        Applicant(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    )
    db.add(
        Representation(
            applicant_id=legal_applicant.id,
            user_id=user.id,
            basis="org_eri",
            valid_from=business_today(),
        )
    )
    await db.flush()
    theirs = await _application(db, legal_applicant, status="INVOICED")

    assert theirs.id in await service.owned_application_ids(db, user)


async def test_a_staff_user_with_no_applicant_row_gets_an_empty_list(db: AsyncSession) -> None:
    staff = await make_user(db, role_code="accountant")
    assert await service.owned_application_ids(db, staff) == []
