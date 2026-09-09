"""Task 7 of stage 7.9: the refund breakdown becomes rows (decision #154),
plus the two pieces `test_refunds.py`'s own rewrite could not pin without
duplicating its fixtures a second time: Override 1's duplicate-source
refusal and `GET /refunds/{id}`'s `available_sources`.

Reuses `test_refunds.py`'s own fixtures (`refund_application`,
`paid_refund_invoice`, `rf01`, `_request_refund`) rather than rebuilding an
approved-and-paid application from scratch — the same cross-file reuse
`test_models_split.py`/`test_refund_sweep.py` already do for `rf01` alone
(mirrors the lesson: a conftest needs plumbing copied from an existing one,
not just fixtures, and here the existing FILE already is that plumbing).

Also imports `head`/`head_client` — the production `executor_head`/
`payments.confirm` checker — for whole-branch-review Important 3:
`test_the_rahbar_can_read_the_refund_he_approves` and
`test_the_rahbar_can_browse_the_refund_register_too` pin that `GET
/refunds/{id}` and `GET /refunds` are now `PAYMENTS_VIEW` OR
`PAYMENTS_CONFIRM`, not `PAYMENTS_VIEW` alone."""

import uuid

from app.modules.applications.models import Application
from app.modules.payments.models import Invoice
from tests.modules.applications.conftest import published_contour as published_contour
from tests.modules.gis.conftest import approval_doc as approval_doc
from tests.modules.gis.conftest import contours_layer as contours_layer
from tests.modules.gis.conftest import leshoz as leshoz
from tests.modules.payments.conftest import BUDGET_RECIPIENT_ID
from tests.modules.payments.test_refunds import REFUNDS, _request_refund
from tests.modules.payments.test_refunds import head as head
from tests.modules.payments.test_refunds import head_client as head_client
from tests.modules.payments.test_refunds import paid_refund_invoice as paid_refund_invoice
from tests.modules.payments.test_refunds import refund_application as refund_application
from tests.modules.payments.test_refunds import rf01 as rf01

# --- Override 1: a duplicate source is refused, NULL included ---------------


async def test_submit_decision_refuses_two_null_recipient_components(
    payments_view_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """`uq_refund_components_source` cannot stop two `recipient_id IS NULL`
    rows — Postgres treats `NULL <> NULL` under a plain UNIQUE constraint —
    so the SERVICE must refuse it before either row is ever written. Two
    components both naming the leshoz's own remainder is the exact case the
    database's own index cannot catch."""
    filed = await _request_refund(payments_view_client, refund_application.id, rf01)
    response = await payments_view_client.post(
        f"{REFUNDS}/{filed['id']}/submit-decision",
        json={
            "final_amount": "600000.00",
            "components": [
                {"recipient_id": None, "amount": "300000.00"},
                {"recipient_id": None, "amount": "300000.00"},
            ],
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "ERR-VAL-001"
    assert body["error"]["details"]["reason"] == "duplicate_source"


async def test_submit_decision_refuses_two_components_naming_the_same_recipient(
    payments_view_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """The non-NULL half of the same rule: two components both naming
    `BUDGET_RECIPIENT_ID` are refused by the SERVICE with the same
    `ERR-VAL-001`/`duplicate_source`, not by an `IntegrityError` 500 out of
    `uq_refund_components_source` (which WOULD catch this one, but a clean
    422 answered before any write is the point of Override 1, uniform
    across NULL and non-NULL sources)."""
    filed = await _request_refund(payments_view_client, refund_application.id, rf01)
    response = await payments_view_client.post(
        f"{REFUNDS}/{filed['id']}/submit-decision",
        json={
            "final_amount": "600000.00",
            "components": [
                {"recipient_id": str(BUDGET_RECIPIENT_ID), "amount": "300000.00"},
                {"recipient_id": str(BUDGET_RECIPIENT_ID), "amount": "300000.00"},
            ],
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "ERR-VAL-001"
    assert body["error"]["details"]["reason"] == "duplicate_source"


# --- Whole-branch review Important 5: a component's source must be one this
# --- invoice actually paid -----------------------------------------------


async def test_submit_decision_refuses_a_recipient_this_invoice_never_paid(
    payments_view_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """Before the fix, `submit_refund_decision` checked the duplicate rule
    and the sum but never that each `recipient_id` was actually one of
    `paid_refund_invoice`'s OWN frozen sources — a stale or mistyped uuid
    reached `flush()` and would either 500 on the FK or, worse, name a
    recipient real elsewhere in `payment_recipients` but never active on
    THIS invoice, which the sum check cannot catch (it only checks the
    total, not who it is attributed to). A random uuid is exactly the
    "stale or mistyped" case the finding names — no row in
    `payment_recipients` needs to exist for this to matter, since
    `available_sources_for` computes the LEGAL set from this invoice's own
    `invoice_recipients` snapshot, not from the directory."""
    filed = await _request_refund(payments_view_client, refund_application.id, rf01)
    bogus_recipient_id = uuid.uuid4()
    response = await payments_view_client.post(
        f"{REFUNDS}/{filed['id']}/submit-decision",
        json={
            "final_amount": "600000.00",
            "components": [
                {"recipient_id": str(bogus_recipient_id), "amount": "600000.00"},
            ],
        },
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "ERR-VAL-001"
    assert body["error"]["details"]["reason"] == "recipient_not_in_split"
    assert body["error"]["details"]["recipient_id"] == str(bogus_recipient_id)


# --- GET /refunds/{id}: available_sources and components ---------------------


async def test_get_refund_offers_the_invoice_s_own_receivers(
    payments_view_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """`available_sources` is the invoice's OWN frozen split
    (`invoice_recipients`), not three fixed buckets: `paid_refund_invoice`
    was paid through the real `confirm_payment` path against the seeded
    `budget_50` recipient (50%) plus the leshoz's own remainder, and this
    reads that exact snapshot back, remainder LAST."""
    filed = await _request_refund(payments_view_client, refund_application.id, rf01)
    response = await payments_view_client.get(f"{REFUNDS}/{filed['id']}")
    assert response.status_code == 200, response.text
    sources = response.json()["available_sources"]
    assert [s["recipient_id"] for s in sources] == [str(BUDGET_RECIPIENT_ID), None]
    assert sources[-1]["kind"] == "remainder"
    assert sources[0]["name"]["uz_latn"] == "Davlat byudjeti"


async def test_get_refund_shows_the_submitted_components_before_approval(
    payments_view_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """`components` reflects whatever `submit_refund_decision` already
    stored — visible to the rahbar's own `GET /refunds/{id}` BEFORE
    `approve` ever runs, with positive amounts (what the accountant
    entered) and the leshoz's account still `None` (not yet resolved —
    resolution only happens once the refund actually reaches
    `returned`)."""
    filed = await _request_refund(payments_view_client, refund_application.id, rf01)
    submitted = await payments_view_client.post(
        f"{REFUNDS}/{filed['id']}/submit-decision",
        json={
            "final_amount": "600000.00",
            "components": [
                {"recipient_id": str(BUDGET_RECIPIENT_ID), "amount": "100000.00"},
                {"recipient_id": None, "amount": "500000.00"},
            ],
        },
    )
    assert submitted.status_code == 200, submitted.text

    response = await payments_view_client.get(f"{REFUNDS}/{filed['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    components = body["components"]
    assert sorted(c["amount"] for c in components) == ["100000.00", "500000.00"]
    assert all(c["account"] is None for c in components)
    budget_component = next(c for c in components if c["recipient_id"] == str(BUDGET_RECIPIENT_ID))
    assert budget_component["name"]["uz_latn"] == "Davlat byudjeti"

    # Minor 6 (whole-branch review): the SAME response's `available_sources`
    # already carries the leshoz's REAL frozen organization name for this
    # identical party (`recipient_id is None`) — before the fix,
    # `components` labelled it with the generic `LESHOZ_SNAPSHOT_NAME`
    # fallback (`{"uz_latn": "Leshoz"}`) instead, one response naming the
    # same party two different ways.
    leshoz_component = next(c for c in components if c["recipient_id"] is None)
    remainder_source = next(s for s in body["available_sources"] if s["recipient_id"] is None)
    assert leshoz_component["name"] == remainder_source["name"]
    assert leshoz_component["name"] != {"uz_latn": "Leshoz"}


# --- Whole-branch review Important 3: the rahbar's own read ------------------


async def test_the_rahbar_can_read_the_refund_he_approves(
    payments_view_client,
    head_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """Before the fix, `GET /refunds/{id}` was `require_permission
    (PAYMENTS_VIEW)`: `head_client` (`executor_head`, `payments.confirm`
    only) got `ERR-ACL-001` here even though he could `POST .../approve`
    on the SAME refund and see `components` in that response — i.e. read
    the breakdown only by committing to it. `available_sources` is what
    this route exists for (its own docstring: "the rahbar's own form
    offers exactly the parties THIS payment was split between"), so the
    rahbar reaching it is the whole point."""
    filed = await _request_refund(payments_view_client, refund_application.id, rf01)

    response = await head_client.get(f"{REFUNDS}/{filed['id']}")
    assert response.status_code == 200, response.text
    sources = response.json()["available_sources"]
    assert [s["recipient_id"] for s in sources] == [str(BUDGET_RECIPIENT_ID), None]


async def test_the_rahbar_can_browse_the_refund_register_too(
    payments_view_client,
    head_client,
    refund_application: Application,
    paid_refund_invoice: Invoice,
    rf01: uuid.UUID,
):
    """`GET /refunds` widened alongside `GET /refunds/{id}` (same
    controller ruling): this docstring already called it "the accountant's/
    rahbar's own register", and it is the only route through which the
    rahbar could ever discover a refund's id to approve — no notification
    hands him one today."""
    filed = await _request_refund(payments_view_client, refund_application.id, rf01)

    response = await head_client.get(REFUNDS, params={"application_id": str(refund_application.id)})
    assert response.status_code == 200, response.text
    ids = [row["id"] for row in response.json()["items"]]
    assert filed["id"] in ids
