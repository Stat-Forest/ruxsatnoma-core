"""Payment intents and the checkout link — `POST /invoices/{id}/pay-intents`
(design/02 § payments, plan `03.10a-payments-core` task 5, design/04 §3.8).
`GetStatement`/`ChangePassword` are Payme RPC methods, not this route, and
live in `test_payme_rpc.py` instead.

Fixture-naming note: `conftest.py` re-exports `gis.conftest`'s
`applicant_client` (an UNRELATED, freshly registered applicant) under its
OWN name — the "stranger" case `test_invoice.py`'s own tests use. The
brief's three tests below need the OPPOSITE pairing for THIS file:
`applicant_client` as the OWNER of `pending_invoice`/`expired_invoice`, a
separate name (the brief's own `other_applicant_client`) for the stranger.
Both are defined LOCALLY here rather than imported under a new name —
`applicant_client` simply forwards `owner_client` (already built for
exactly the owner shape); `other_applicant_client` duplicates
`gis.conftest`'s own `applicant_client` construction rather than
depending on it BY NAME, which this file's own `applicant_client` now
shadows. A plain renamed import (`owner_client as applicant_client`) was
tried first and rejected: ruff's F811 flags a test-function parameter
shadowing an IMPORTED name as a redefinition, even though the identical
shape is silent for a locally-DEFINED fixture (lesson: 'A re-exported
fixture shadowed by a same-file parameter trips ruff's F811') — the same
class of trip, just via a rename instead of a same-name re-export."""

import base64
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import business_today
from app.main import create_app
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, Representation
from app.modules.payments.models import Invoice
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import _commit_pending_before_requests

API = "/api/v1"


def _pinfl() -> str:
    # Leading digit 8: 1-7 are already claimed by other test modules sharing
    # this same persistent test DB (see tests/modules/gis/conftest.py's own
    # comment on the same convention).
    return f"8{uuid.uuid4().int % 10**13:013d}"


@pytest.fixture
def applicant_client(owner_client):
    """This file's own vocabulary: the brief's success/expired-refusal
    tests below call the invoice OWNER's client `applicant_client` —
    `owner_client` (`conftest.py`) is the identical concept under the name
    `test_invoice.py`'s 'owner vs stranger' tests use instead."""
    return owner_client


@pytest.fixture
async def other_applicant_client(db: AsyncSession):
    """The brief's own name for a fully registered, UNRELATED applicant —
    same construction as `gis.conftest`'s own `applicant_client`,
    duplicated rather than depended on by that name: THIS file's own
    `applicant_client` (above) means the invoice owner instead, so a
    fixture parameter named `applicant_client` here would resolve to that
    one, not the original."""
    user = await make_user(db, role_code="applicant", pinfl=_pinfl())
    db.add(
        Applicant(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    )
    await db.flush()
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


async def test_the_checkout_url_carries_the_merchant_the_invoice_and_the_amount_in_tiyin(
    applicant_client, pending_invoice
):
    result = await applicant_client.post(
        f"/api/v1/invoices/{pending_invoice.id}/pay-intents",
        json={"provider": "payme"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert result.status_code == 201, result.text
    url = result.json()["payment_url"]
    assert url.startswith("https://checkout.paycom.uz/")

    decoded = base64.b64decode(url.rsplit("/", 1)[1]).decode()
    assert f"ac.id={pending_invoice.number}" in decoded
    assert f"a={int(pending_invoice.amount * 100)}" in decoded


async def test_paying_an_expired_invoice_is_refused(applicant_client, expired_invoice):
    result = await applicant_client.post(
        f"/api/v1/invoices/{expired_invoice.id}/pay-intents",
        json={"provider": "payme"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "ERR-PAY-002"


async def test_another_applicant_cannot_start_a_payment_for_my_invoice(
    other_applicant_client, pending_invoice
):
    result = await other_applicant_client.post(
        f"/api/v1/invoices/{pending_invoice.id}/pay-intents",
        json={"provider": "payme"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert result.status_code == 404


# --- review finding: a settled invoice must refuse a new intent, not just an
# expired one. `due_at` alone (ERR-PAY-002) never catches an invoice that was
# already paid or cancelled WHILE still inside its own window.


@pytest.fixture
async def paid_invoice(db: AsyncSession, pending_invoice: Invoice) -> Invoice:
    """`pending_invoice` marked paid directly — this fixture exists only to
    prove `create_pay_intent`'s OWN `ERR-PAY-004` status check, not to
    re-prove the real `PerformTransaction` path (`test_payme_rpc.py` already
    does that exhaustively), so a direct field set is legitimate here.
    `due_at` stays whatever `issue_invoice`'s real "+10 days" rule gave
    `pending_invoice` — still well inside the window, the exact "settled but
    not expired" shape the finding is about."""
    pending_invoice.status = "paid"
    pending_invoice.paid_at = datetime.now(UTC)
    await db.flush()
    return pending_invoice


async def test_paying_an_already_paid_invoice_is_refused(applicant_client, paid_invoice):
    result = await applicant_client.post(
        f"/api/v1/invoices/{paid_invoice.id}/pay-intents",
        json={"provider": "payme"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "ERR-PAY-004"


async def test_paying_a_cancelled_invoice_is_refused(applicant_client, cancelled_invoice):
    """`cancelled_invoice` (`conftest.py`) is `pending_invoice` cancelled
    through the real event path — its `due_at` is likewise still in the
    future, so this is refused on STATUS, not on the date."""
    result = await applicant_client.post(
        f"/api/v1/invoices/{cancelled_invoice.id}/pay-intents",
        json={"provider": "payme"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "ERR-PAY-004"


# --- ownership ruling: an effective representative may act too --------------
# Task 2 shipped the invoice routes admitting only the applicant's own
# `owner_user_id`, leaving a legal entity's non-owner representative unable
# to see or pay an invoice they filed themselves — deliberately carried to
# this task because the pay-intent route reuses the same check
# (`service._may_act_on_invoices_of`). These fixtures build a LEGAL
# applicant (the only kind a `Representation` means anything for —
# `Applicant.owner_user_id` is `None` by construction for `kind='legal'`,
# decision #9) rather than reusing `pending_invoice`'s own individual
# `applicant`.


def _stir() -> str:
    """A fresh, valid-shape (`^[0-9]{9}$`) stir per call — `applicants.stir`
    is UNIQUE (same reasoning as `_pinfl()` above)."""
    return f"{secrets.randbelow(10**9):09d}"


@pytest.fixture
async def legal_applicant(db: AsyncSession) -> Applicant:
    row = Applicant(kind="legal", stir=_stir(), name="OOO Represented")
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def legal_invoice(db: AsyncSession, legal_applicant: Applicant) -> Invoice:
    """A pending invoice for `legal_applicant`, built directly — Task 1's
    own model-level idiom (mirrors `conftest.py::invoice`) — since this
    fixture exists only to give the representative test an invoice whose
    OWNING APPLICANT is `kind='legal'`."""
    submitter = await make_user(db, role_code="applicant", pinfl=_pinfl())
    application = Application(
        applicant_id=legal_applicant.id,
        submitted_by_user_id=submitter.id,
        on_behalf="legal",
        channel="portal",
        status="APPROVED",
    )
    db.add(application)
    await db.flush()
    # Invoice.due_at defaults to func.now() at the schema level (model
    # docstring: "the real '+10 days' rule is the issuing service's job to
    # compute and pass explicitly") — left at the default, due_at could
    # already read as "past due" by the time the HTTP request below runs.
    now = datetime.now(UTC)
    row = Invoice(
        application_id=application.id,
        number=f"INV-2027-{uuid.uuid4().hex[:6]}",
        amount=Decimal("100.00"),
        status="pending",
        issued_at=now,
        due_at=now + timedelta(days=10),
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def representative_client(db: AsyncSession, legal_applicant: Applicant):
    """A user who is themselves a registered individual applicant (required
    by `get_current_user`'s own `ERR-AUTH-008` gate on any `applicant`-role
    account with no `Applicant` row of its own — decision #9's
    representatives are real accounts, not bare grants) AND holds an
    ACTIVE `Representation` over `legal_applicant`, `basis='org_eri'`
    (needs no `poa_file_id`/`valid_until` — the DB CHECK only requires those
    for `basis='poa'`)."""
    user = await make_user(db, role_code="applicant", pinfl=_pinfl())
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
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


async def test_a_representative_can_start_a_payment_for_the_applicant_they_represent(
    representative_client, legal_invoice
):
    result = await representative_client.post(
        f"{API}/invoices/{legal_invoice.id}/pay-intents",
        json={"provider": "payme"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert result.status_code == 201, result.text
    assert result.json()["payment_url"].startswith("https://checkout.paycom.uz/")
