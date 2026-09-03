"""`admin.service.parent_organization` — one step up the organization tree
(plan 03.9a task 7, ruling 24).

Written for `applications`' over-limit forward (decision #29: «эскалируется
ваколатли шахсу вышестоящей организации»), and it lives in `admin.service`
rather than in the caller because reference data is read through this module's
own service — a private `SELECT parent_id` in `applications.repo` would be a
boundary violation however small it looks.

Three cases, and the third is the one the escalation actually turns on: at the
agency there is nowhere to go, and `None` is the answer the caller has to be
able to refuse loudly on.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.db import uuid7
from app.modules.admin import service
from app.modules.admin.models import Organization


@pytest.fixture
async def leshoz_under_territorial(db: AsyncSession, agency: Organization) -> Organization:
    """agency -> territorial -> leshoz, the middle stretch of ruling 6's chain,
    so the test can walk two steps and land on the root."""
    territorial = Organization(
        id=uuid7(),
        kind="territorial",
        code=f"TD{uuid.uuid4().hex[:8]}",
        name={"uz_cyrl": "Ҳудудий бошқарма", "en": "Territorial department"},
        parent_id=agency.id,
    )
    db.add(territorial)
    await db.flush()
    leshoz = Organization(
        id=uuid7(),
        kind="leshoz",
        code=f"LH{uuid.uuid4().hex[:8]}",
        name={"uz_cyrl": "Ўрмон хўжалиги", "en": "Leshoz"},
        parent_id=territorial.id,
    )
    db.add(leshoz)
    await db.flush()
    return leshoz


async def test_the_parent_of_a_leshoz_is_its_territorial_department(
    db: AsyncSession, leshoz_under_territorial: Organization
) -> None:
    parent = await service.parent_organization(db, leshoz_under_territorial.id)
    assert parent is not None
    assert parent.id == leshoz_under_territorial.parent_id
    assert parent.kind == "territorial"


async def test_the_agency_has_no_parent(db: AsyncSession, agency: Organization) -> None:
    """`ck_organizations_root_is_agency` makes `parent_id IS NULL` and
    `kind = 'agency'` the same fact, so this is the top of every ladder — and
    `None` here is what makes an unresolvable escalation refusable rather than
    silent."""
    assert await service.parent_organization(db, agency.id) is None


async def test_an_unknown_organization_is_404_not_a_silent_none(db: AsyncSession) -> None:
    """`None` means "this is the root", and nothing else. An id that names no
    organization has to be distinguishable from it — otherwise a caller reading
    `None` as "escalate nowhere" would answer «no parent organization» for a
    plain typo."""
    with pytest.raises(DomainError) as raised:
        await service.parent_organization(db, uuid7())
    assert raised.value.code == "ERR-SYS-003"
