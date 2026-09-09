"""Task 6 of plan `07.9-payme-split` (decision #160): `CreateTransaction`'s
response gains `receivers`, and BOTH `CheckPerformTransaction` and
`CreateTransaction` refuse a payment whose frozen split cannot be routed at
Payme in full — `-31008`, ALWAYS answered HTTP 200 (the Payme envelope
carries the refusal, never the status code).

The brief's own seven tests, verbatim, plus a handful the controller's own
self-review asked for by name: whether the tests would catch a PARTIAL
`receivers` array (`invoice_with_budget_lacking_an_id`, below, is
deliberately partial — one configured receiver has an id, the seeded
`budget_50` does not — never a shape where nothing has one at all, which
would not distinguish "partial" from "empty"), and the lesson "Two
mechanisms refusing one thing: an outcome-only test cannot tell which one
fired" (`_check_invoice_for_payment`'s own status check ALSO answers
`-31008`, for an unrelated reason — a non-pending invoice — so a code-only
assertion on a PENDING invoice does not by itself prove the missing-id
branch is what ran; `test_the_missing_id_refusal_names_the_right_reason`
pins the message too).

Every fixture below deactivates the seeded `budget_50` (`budget_50_inactive`,
Task 3) before adding its OWN configured receivers, or clears `budget_50`'s
own Payme id directly — never relies on `conftest.py`'s package-wide
`_budget_recipient_is_routable` autouse fixture (which exists solely so
every OTHER, pre-existing fixture in this package stays payable under this
task's new routability check): this file's whole point is the refusal, so
its own fixtures build their split from scratch rather than inheriting a
default meant for tests that predate the concept entirely.

`_deactivate_custom_receivers_after` (below) is this file's OWN version of
`test_recipients_api.py`'s `_reset_payment_recipients`: every test here
that calls `payme_rpc` drives a REAL HTTP request, and `client`'s own
`_commit_pending_before_requests` commits whatever `receiver_a`/`receiver_b`
staged on `db` FOR REAL before that request — so, unlike a plain `db`-only
test (which rolls back at teardown), these two rows would otherwise survive
as ACTIVE `payment_recipients` rows for the rest of this worker's run and
push a LATER test's own split past 100% (`SplitDoesNotFit`, the exact
cross-file pollution `test_recipients_api.py`'s own `DELETE ... WHERE id !=
BUDGET_RECIPIENT_ID` already risks — see its docstring). Deactivates rather
than deletes: a row `invoice_with_complete_snapshot` already froze into an
`invoice_recipients` row cannot be deleted at all (`fk_invoice_recipients_
recipient_id_payment_recipients`), and an inactive leftover is harmless —
`list_active_recipients` never reads it again."""

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import make_session_factory
from app.modules.applications import service as applications_service
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant
from app.modules.integrations.adapters.payme import to_tiyin
from app.modules.norms.models import Calculation
from app.modules.payments import service
from app.modules.payments.models import Invoice, PaymentRecipient
from tests.modules.applications.conftest import grazing_activity_id as grazing_activity_id
from tests.modules.payments.conftest import PAYME_TEST_CASHBOX_KEY, _new_approved_application
from tests.modules.payments.test_confirm_payment_split import manual_confirm
from tests.modules.payments.test_payme_rpc import _auth

# Two configured receivers' TEST-ONLY Payme account ids — the brief's own
# literals, so `test_create_transaction_returns_receivers_when_every_id_is_
# known`'s exact expected list is reachable.
RECEIVER_A_PAYME_ID = "99999"
RECEIVER_B_PAYME_ID = "12345"


@pytest.fixture(autouse=True)
async def _deactivate_custom_receivers_after(engine) -> AsyncIterator[None]:
    """See the module docstring. Through `engine`/`make_session_factory`,
    never `db` (mirrors `conftest.py`'s own `_budget_recipient_is_routable`
    and `budget_50_inactive`): a row committed by the APP's separate
    connection needs a write on that SAME footing to reach it, and `db`'s
    own rollback at teardown only ever undoes what it never committed."""
    yield
    factory = make_session_factory(engine)
    async with factory() as session:
        await session.execute(
            update(PaymentRecipient)
            .where(
                PaymentRecipient.payme_account_id.in_([RECEIVER_A_PAYME_ID, RECEIVER_B_PAYME_ID])
            )
            .values(active=False)
        )
        await session.commit()


async def payme_rpc(
    client: httpx.AsyncClient, method: str, invoice: Invoice, *, key: str = PAYME_TEST_CASHBOX_KEY
) -> dict[str, Any]:
    """One JSON-RPC call to `/webhooks/payme` for `invoice`'s own account
    number and amount, returning the parsed body PLUS `_http_status`
    (Override 1: a test must assert the STATUS CODE is 200 on the refusal
    path, not only that the error object inside it is right — the JSON
    body alone carries no status code of its own).

    `id` is DERIVED from `invoice.id`, never `test_payme_rpc.py`'s own
    per-call `_tx_id()`: Override 5's own repeat test
    (`test_a_repeat_returns_the_same_receivers`) calls this TWICE for the
    SAME invoice and must drive `CreateTransaction`'s real
    idempotent-replay branch — a fresh random id on the second call would
    silently create a SECOND transaction row instead of proving the
    retry. Still never a FIXED literal (lesson: "the test DB is shared,
    persistent, and never empty"): `invoice.id` is itself a fresh `uuid7`
    every time a fixture builds one, so this stays unique per test run."""
    response = await client.post(
        "/api/v1/webhooks/payme",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {
                "id": f"payme-receivers-{invoice.id}",
                "amount": to_tiyin(invoice.amount),
                "account": {"id": invoice.number},
            },
        },
        headers=_auth(key),
    )
    body = response.json()
    body["_http_status"] = response.status_code
    return body


@pytest.fixture
async def approved_application_600k(
    db: AsyncSession, applicant: Applicant, grazing_activity_id: uuid.UUID
) -> Application:
    """An APPROVED application priced at 600 000 — round enough that two
    50% rules both land on a whole tiyin. No contour, no assigned
    organization: every scenario below either consumes the WHOLE invoice
    across configured receivers (leaving the leshoz's own remainder at
    exactly `0.00`, Override 6 — its missing Payme id can never be why a
    payment is refused) or is refused before the leshoz's own id would
    ever matter."""
    row = await _new_approved_application(db, applicant)
    db.add(
        Calculation(
            application_id=row.id,
            activity_type_id=grazing_activity_id,
            rule_code_version="norms-1.0.0",
            input_snapshot={},
            amount=Decimal("600000.00"),
            breakdown={},
        )
    )
    await db.flush()
    return row


@pytest.fixture
async def receiver_a(db: AsyncSession) -> PaymentRecipient:
    """50%, `sort_order=10` — first in the frozen snapshot."""
    row = PaymentRecipient(
        name={"uz_latn": "Birinchi qabul qiluvchi"},
        kind="percent",
        percent=Decimal("50.00"),
        payme_account_id=RECEIVER_A_PAYME_ID,
        sort_order=10,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def receiver_b(db: AsyncSession) -> PaymentRecipient:
    """50%, `sort_order=20` — second, AFTER `receiver_a`."""
    row = PaymentRecipient(
        name={"uz_latn": "Ikkinchi qabul qiluvchi"},
        kind="percent",
        percent=Decimal("50.00"),
        payme_account_id=RECEIVER_B_PAYME_ID,
        sort_order=20,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def invoice_with_complete_snapshot(
    db: AsyncSession,
    approved_application_600k: Application,
    budget_50_inactive: PaymentRecipient,
    receiver_a: PaymentRecipient,
    receiver_b: PaymentRecipient,
) -> Invoice:
    """Two configured receivers, 50% each, consuming the WHOLE invoice —
    `budget_50_inactive` (Task 3) keeps the seeded `budget_50` OUT of this
    split, or three active rules would total 150% and `issue_invoice`
    would refuse with `SplitDoesNotFit` before this fixture ever returns.
    The leshoz's own remainder is therefore exactly `0.00` and carries no
    Payme id (no organization resolves for this application) — Override
    6: `receivers` names exactly `receiver_a`/`receiver_b`, in
    `sort_order` order, never a third row for a zero-amount leshoz."""
    return await service.issue_invoice(db, approved_application_600k.id)


@pytest.fixture
async def invoice_with_budget_lacking_an_id(
    db: AsyncSession,
    approved_application_600k: Application,
    budget_50: PaymentRecipient,
    receiver_b: PaymentRecipient,
) -> Invoice:
    """A genuinely PARTIAL split — the point of this fixture's own name,
    and of `test_the_manual_maker_checker_path_is_not_blocked`'s carve-out:
    the seeded `budget_50` (50%) carries NO Payme id (cleared here
    regardless of what `conftest.py`'s package-wide
    `_budget_recipient_is_routable` autouse fixture already set it to,
    undoing that default on purpose for this ONE deliberately-unrouted
    scenario) while `receiver_b` (the OTHER 50%) has one. All-or-nothing
    (Override 6): the ONE missing id refuses the WHOLE payment — never a
    shortened `receivers` array naming only `receiver_b` — which is
    exactly what distinguishes this fixture from one where NOTHING has an
    id at all."""
    budget_50.payme_account_id = None
    await db.flush()
    return await service.issue_invoice(db, approved_application_600k.id)


@pytest.fixture
async def legacy_invoice_no_snapshot(
    db: AsyncSession, approved_application: Application
) -> Invoice:
    """An invoice shaped like one issued BEFORE this stage: the application
    reached INVOICED and the invoice row exists, but nothing ever wrote an
    `invoice_recipients` snapshot for it — built directly, bypassing
    `issue_invoice` (the ONLY path that writes one), the same idiom
    `test_confirm_payment_split.py::legacy_invoice` uses for the identical
    historical shape (Override 3: `service.invoice_recipients` returns
    `[]` for it, and it stays payable unchanged)."""
    await applications_service.set_status(db, approved_application.id, to_status="INVOICED")
    row = Invoice(
        application_id=approved_application.id,
        number=f"INV-2027-{uuid.uuid4().hex[:6]}",
        amount=Decimal("100.00"),
        status="pending",
    )
    db.add(row)
    await db.flush()
    return row


# --- the brief's own seven tests, verbatim -----------------------------------


async def test_create_transaction_returns_receivers_when_every_id_is_known(
    client, invoice_with_complete_snapshot
):
    response = await payme_rpc(client, "CreateTransaction", invoice_with_complete_snapshot)
    assert response["result"]["receivers"] == [
        {"id": "99999", "amount": 30000000},
        {"id": "12345", "amount": 30000000},
    ]


async def test_a_missing_id_refuses_the_payment_rather_than_taking_it(
    client, invoice_with_budget_lacking_an_id
):
    response = await payme_rpc(client, "CreateTransaction", invoice_with_budget_lacking_an_id)
    assert response["error"]["code"] == -31008
    assert "receivers" not in str(response)
    # Still HTTP 200 - the Payme envelope carries the refusal, never the status code.
    assert response["_http_status"] == 200


async def test_check_perform_refuses_first_so_nobody_reaches_the_payment_page(
    client, invoice_with_budget_lacking_an_id
):
    response = await payme_rpc(client, "CheckPerformTransaction", invoice_with_budget_lacking_an_id)
    assert response["error"]["code"] == -31008


async def test_an_invoice_issued_before_this_stage_still_pays(client, legacy_invoice_no_snapshot):
    response = await payme_rpc(client, "CreateTransaction", legacy_invoice_no_snapshot)
    assert response["result"]["state"] == 1
    assert "receivers" not in response["result"]


async def test_the_manual_maker_checker_path_is_not_blocked(db, invoice_with_budget_lacking_an_id):
    # Ruling R1 carve-out 2: that money arrived by bank transfer and never
    # touched Payme. The ledger records the split even though nothing can route it.
    await manual_confirm(db, invoice_with_budget_lacking_an_id, amount=Decimal("600000.00"))
    entries = await service.allocations_for(db, invoice_with_budget_lacking_an_id.id)
    assert sum(e.amount for e in entries) == Decimal("600000.00")


async def test_the_receivers_amounts_total_the_transaction_amount(
    client, invoice_with_complete_snapshot
):
    result = (await payme_rpc(client, "CreateTransaction", invoice_with_complete_snapshot))[
        "result"
    ]
    assert sum(r["amount"] for r in result["receivers"]) == 60000000


async def test_a_repeat_returns_the_same_receivers(client, invoice_with_complete_snapshot):
    first = await payme_rpc(client, "CreateTransaction", invoice_with_complete_snapshot)
    second = await payme_rpc(client, "CreateTransaction", invoice_with_complete_snapshot)
    assert first["result"] == second["result"]


# --- additional coverage (self-review: message-level proof, and the exact
# zero-amount exemption Override 6 describes) --------------------------------


async def test_the_missing_id_refusal_names_the_right_reason(
    client, invoice_with_budget_lacking_an_id
):
    """Lesson: "A green test proves nothing until you have seen it go red" —
    an outcome-only assertion cannot tell which mechanism refused.
    `_check_invoice_for_payment`'s own
    status check ALSO answers `-31008`, for an unrelated reason (a
    non-pending invoice) — `invoice_with_budget_lacking_an_id` is
    `pending`, so a code-only assertion does not by itself prove the
    missing-id branch (`_receivers_for`) is what ran rather than some
    other refusal reusing the same code. Revert `_receivers_for`'s own
    raise to prove this goes red."""
    response = await payme_rpc(client, "CreateTransaction", invoice_with_budget_lacking_an_id)
    assert response["error"]["message"] == "Split cannot be routed"


async def test_a_zero_amount_leshoz_remainder_with_no_id_never_blocks_payment(
    db, client, invoice_with_complete_snapshot
):
    """Override 6's other half, pinned directly rather than left as an
    implicit byproduct of the "every id is known" test passing: the
    leshoz's own remainder row in `invoice_with_complete_snapshot` is
    exactly `0.00` (the two configured receivers already consume 100%)
    AND carries no Payme id (no organization resolves for this
    application) — proving a receiver Payme would never be asked to route
    anything to can never be the reason the whole payment is refused."""
    snapshot = await service.invoice_recipients(db, invoice_with_complete_snapshot.id)
    leshoz_row = snapshot[-1]
    assert leshoz_row.kind == "remainder"
    assert leshoz_row.amount == Decimal("0.00")
    assert leshoz_row.payme_account_id is None

    response = await payme_rpc(client, "CreateTransaction", invoice_with_complete_snapshot)
    assert response["result"]["state"] == 1
    assert "error" not in response
