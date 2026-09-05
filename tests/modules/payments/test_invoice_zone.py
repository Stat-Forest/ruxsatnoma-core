"""`tz/12` #35, answered by Oybek on 2026-09-05: an accountant belongs to a
leshoz, so an invoice is territorial like every other row.

Before this, `_may_act_on_invoices_of` asked one question — does the caller
hold `payments.view` — and answered `true` for any invoice in the country.
`payments.view` and `payments.manage` belong to `accountant` alone (migration
0017), and the same predicate guards the pay-intent route, so an accountant
attached to one leshoz could open AND pay another leshoz's invoice.

The refusal is `ERR-SYS-003` (404) rather than `ERR-ACL-002` (403), because
that is what the two invoice read routes already answer a stranger: a 403
would confirm to somebody outside the zone that the invoice exists. The
pay-intent route follows the same rule for the same reason.
"""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import uuid7
from app.modules.admin.models import Organization
from app.modules.payments.models import Invoice
from tests.modules.auth.test_sessions import make_user
from tests.modules.payments.conftest import (
    _commit_pending_before_requests,
    auth_client,
    create_app,
    make_client,
    make_session,
)


async def _agency(db: AsyncSession) -> Organization:
    """The single-agency partial unique index makes this row a singleton the
    shared, persistent test DB may already carry — reuse it rather than assume
    a fresh database (lesson)."""
    existing = (
        await db.execute(select(Organization).where(Organization.kind == "agency"))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    agency = Organization(
        id=uuid7(),
        code=f"A{uuid.uuid4().hex[:8]}",
        kind="agency",
        name={"uz_cyrl": "Агентлик", "uz_latn": "Agentlik"},
    )
    db.add(agency)
    await db.flush()
    return agency


async def _leshoz(db: AsyncSession, label: str) -> Organization:
    agency = await _agency(db)
    org = Organization(
        id=uuid7(),
        parent_id=agency.id,
        code=f"L{uuid.uuid4().hex[:8]}",
        kind="leshoz",
        name={"uz_cyrl": label, "uz_latn": label},
    )
    db.add(org)
    await db.flush()
    return org


@pytest.fixture
async def home_leshoz(db: AsyncSession) -> Organization:
    return await _leshoz(db, "Burchmulla")


@pytest.fixture
async def other_leshoz(db: AsyncSession) -> Organization:
    return await _leshoz(db, "Chimyon")


async def _accountant_in(db: AsyncSession, org: Organization) -> AsyncIterator[httpx.AsyncClient]:
    """THE production `accountant` role — not an actor with the code bolted on
    — attached to one leshoz. Mirrors `payments_view_client`, which is
    deliberately zone-free."""
    user = await make_user(db, role_code="accountant", organization_id=org.id)
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


@pytest.fixture
async def home_accountant(
    db: AsyncSession, home_leshoz: Organization
) -> AsyncIterator[httpx.AsyncClient]:
    async for client in _accountant_in(db, home_leshoz):
        yield client


@pytest.fixture
async def other_accountant(
    db: AsyncSession, other_leshoz: Organization
) -> AsyncIterator[httpx.AsyncClient]:
    async for client in _accountant_in(db, other_leshoz):
        yield client


@pytest.fixture
async def invoice_in_home_leshoz(
    db: AsyncSession, invoice: Invoice, home_leshoz: Organization
) -> Invoice:
    """The invoice's application assigned to a known leshoz. `assigned_org_id`
    is what `applications.service` resolves first when placing an application
    in a zone; the contour's owner is the fallback, and this fixture's
    application has no contour."""
    from app.modules.applications.models import Application

    application = await db.get(Application, invoice.application_id)
    assert application is not None
    application.assigned_org_id = home_leshoz.id
    await db.flush()
    await db.commit()
    return invoice


async def test_an_accountant_of_another_leshoz_cannot_open_the_invoice(
    other_accountant: httpx.AsyncClient, invoice_in_home_leshoz: Invoice
) -> None:
    response = await other_accountant.get(f"/api/v1/invoices/{invoice_in_home_leshoz.id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR-SYS-003"


async def test_the_accountant_of_that_leshoz_opens_it(
    home_accountant: httpx.AsyncClient, invoice_in_home_leshoz: Invoice
) -> None:
    response = await home_accountant.get(f"/api/v1/invoices/{invoice_in_home_leshoz.id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(invoice_in_home_leshoz.id)


async def test_an_accountant_of_another_leshoz_cannot_list_the_application_invoices(
    other_accountant: httpx.AsyncClient, invoice_in_home_leshoz: Invoice
) -> None:
    response = await other_accountant.get(
        "/api/v1/invoices", params={"application_id": str(invoice_in_home_leshoz.application_id)}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR-SYS-003"


async def test_an_accountant_of_another_leshoz_cannot_start_a_payment(
    other_accountant: httpx.AsyncClient, invoice_in_home_leshoz: Invoice
) -> None:
    """The read routes and the pay-intent route share one predicate, so the
    zone has to hold on the route that MOVES MONEY, not only on the ones that
    show it."""
    response = await other_accountant.post(
        f"/api/v1/invoices/{invoice_in_home_leshoz.id}/pay-intents",
        json={"provider": "payme"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_republic_wide_accountant_still_sees_every_invoice(
    payments_view_client: httpx.AsyncClient, invoice_in_home_leshoz: Invoice
) -> None:
    """A central accountant — the existing zone-free fixture — keeps seeing
    everything. Answering #35 with "territorial" does not abolish the
    republic-wide case; it makes it a deliberate empty zone rather than the
    only behaviour available."""
    response = await payments_view_client.get(f"/api/v1/invoices/{invoice_in_home_leshoz.id}")

    assert response.status_code == 200


async def test_the_owner_sees_their_own_invoice_regardless_of_zone(
    owner_client: httpx.AsyncClient, invoice_in_home_leshoz: Invoice
) -> None:
    """The citizen's own branch is untouched: ownership is not territorial,
    and a zone rule that swallowed it would hide a person's own bill."""
    response = await owner_client.get(f"/api/v1/invoices/{invoice_in_home_leshoz.id}")

    assert response.status_code == 200
