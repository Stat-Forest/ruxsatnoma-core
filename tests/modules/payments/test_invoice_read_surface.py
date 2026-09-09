"""Stage 7.9 task 8: the read surfaces stop describing two halves.

`InvoiceOut.recipients` (`GET /invoices/{id}`) and `AllocationOut.
recipient_name` (`GET /payments/allocations`), both gated on
`payments.view`. **Override 4 is the one thing here with a human on the
other side**: an applicant paying their own invoice must never be shown
who receives the money — that is internal allocation, not part of what
they are paying for. `test_an_applicant_sees_the_total_only_not_the_split`
pins it.

Fixtures below reuse `test_confirm_payment_split.py`'s own `invoice_600k`
(the seeded `budget_50` alone active, 50% -> 300 000/300 000) verbatim —
pytest resolves a fixture's own parameters against the CURRENT test's
closure, not the file it was defined in (that file's own module
docstring), so `application_600k` is re-exported here too."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import Applicant, User
from tests.modules.auth.test_sessions import make_session
from tests.modules.payments.conftest import _session_headers
from tests.modules.payments.test_confirm_payment_split import (
    PaidInvoiceCtx,
    manual_confirm,
)
from tests.modules.payments.test_confirm_payment_split import (
    application_600k as application_600k,
)
from tests.modules.payments.test_confirm_payment_split import (
    invoice_600k as invoice_600k,
)

API = "/api/v1"


@pytest.fixture
async def applicant_headers(db: AsyncSession, applicant: Applicant) -> dict[str, str]:
    """The OWNER of `application_600k`'s own applicant identity, as session
    headers rather than a whole client — `_session_headers`'s own
    reasoning: one test needs to compare TWO different actors against the
    identical route, which a single signed-in client (`owner_client`)
    cannot express. Mirrors `owner_client`'s own lookup, header-shaped."""
    assert applicant.owner_user_id is not None  # the `applicant` fixture always sets it
    user = await db.get(User, applicant.owner_user_id)
    assert user is not None
    _, token, csrf = await make_session(db, user)
    return _session_headers(token, csrf)


@pytest.fixture
async def paid_invoice_ctx(db: AsyncSession, invoice_600k) -> PaidInvoiceCtx:
    """`invoice_600k` (`budget_50` alone active, 300 000/300 000) paid in
    FULL through the real `confirm_payment` — exactly TWO allocations, the
    configured receiver and the leshoz's own remainder, what
    `test_the_allocations_listing_names_each_receiver` needs
    `recipient_name` to distinguish."""
    transaction = await manual_confirm(db, invoice_600k, amount=invoice_600k.amount)
    return PaidInvoiceCtx(invoice=invoice_600k, transaction=transaction)


async def test_an_applicant_sees_the_total_only_not_the_split(
    client, applicant_headers, invoice_600k
):
    response = await client.get(f"{API}/invoices/{invoice_600k.id}", headers=applicant_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["amount"] == "600000.00"
    assert "recipients" not in body


async def test_an_accountant_sees_how_the_invoice_divides(client, accountant, invoice_600k):
    response = await client.get(f"{API}/invoices/{invoice_600k.id}", headers=accountant)
    assert response.status_code == 200
    body = response.json()
    assert [(r["kind"], r["amount"]) for r in body["recipients"]] == [
        ("percent", "300000.00"),
        ("remainder", "300000.00"),
    ]
    assert body["recipients"][0]["name"]["uz_latn"] == "Davlat byudjeti"


async def test_the_allocations_listing_names_each_receiver(client, accountant, paid_invoice_ctx):
    response = await client.get(
        f"{API}/payments/allocations",
        params={"invoice_id": str(paid_invoice_ctx.invoice.id)},
        headers=accountant,
    )
    assert response.status_code == 200
    body = response.json()
    names = [a["recipient_name"] for a in body["items"]]
    assert names[0]["uz_latn"] == "Davlat byudjeti"
    assert names[1] is None
