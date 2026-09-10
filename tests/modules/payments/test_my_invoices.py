"""`GET /invoices` without `?application_id=` for a caller who is NOT staff
(stage 11, ruling R1): their OWN invoices — their individual row's and every
effectively represented legal entity's — in every status. For staff the same
route is still the register (`test_invoice_zone.py`).

The legal-entity fixtures are `test_intents.py`'s own (the same import idiom
`conftest.py` uses for `test_organizations_admin.auth_client`)."""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.modules.payments.models import Invoice
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import _commit_pending_before_requests
from tests.modules.payments.test_intents import legal_applicant as legal_applicant
from tests.modules.payments.test_intents import legal_invoice as legal_invoice
from tests.modules.payments.test_intents import representative_client as representative_client

INVOICES = "/api/v1/invoices"


async def test_the_owner_lists_every_invoice_of_their_own_in_every_status(
    owner_client: httpx.AsyncClient, cancelled_invoice: Invoice, legal_invoice: Invoice
) -> None:
    """`cancelled_invoice` is a `pending_invoice` cancelled through the real
    event path, so the list holds a cancelled row — history, not just what
    is in force. `legal_invoice` belongs to somebody else and must be absent."""
    response = await owner_client.get(INVOICES)

    assert response.status_code == 200, response.text
    body = response.json()
    ids = {item["id"] for item in body["items"]}
    assert str(cancelled_invoice.id) in ids
    assert str(legal_invoice.id) not in ids
    assert body["total"] == len(body["items"])
    assert {"page", "page_size"} <= body.keys()


async def test_a_representative_lists_the_legal_entitys_invoice(
    representative_client: httpx.AsyncClient, legal_invoice: Invoice
) -> None:
    response = await representative_client.get(INVOICES)

    assert response.status_code == 200, response.text
    assert str(legal_invoice.id) in {item["id"] for item in response.json()["items"]}


async def test_a_stranger_gets_an_empty_page_not_somebody_elses_invoice(
    applicant_client: httpx.AsyncClient, invoice: Invoice
) -> None:
    """`applicant_client` (gis conftest) is a fully registered applicant
    unrelated to `invoice`'s owner — and no longer a 403: an applicant with
    nothing simply has nothing."""
    response = await applicant_client.get(INVOICES)

    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "total": 0, "page": 1, "page_size": 50}


async def test_the_own_list_honours_the_status_filter(
    owner_client: httpx.AsyncClient, cancelled_invoice: Invoice
) -> None:
    response = await owner_client.get(f"{INVOICES}?status=pending")

    assert response.status_code == 200, response.text
    assert all(item["status"] == "pending" for item in response.json()["items"])
    assert str(cancelled_invoice.id) not in {item["id"] for item in response.json()["items"]}


async def test_a_role_with_no_payments_right_and_no_applicant_row_gets_an_empty_page(
    db: AsyncSession, invoice: Invoice
) -> None:
    """The third kind of caller: staff WITHOUT `payments.view`/`.confirm` (a
    GIS specialist, an inspector). Not the register — they hold no right to
    it — and not a 403 either: the own branch answers them the nothing they
    own. Pins that the branch is "not staff-with-a-right", never "is an
    applicant"."""
    user = await make_user(db, role_code="gis_specialist")
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        response = await client.get(INVOICES)

    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
