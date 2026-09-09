"""Task 7 of stage 7.9: the refund breakdown becomes rows (decision #154),
plus the two pieces `test_refunds.py`'s own rewrite could not pin without
duplicating its fixtures a second time: Override 1's duplicate-source
refusal and `GET /refunds/{id}`'s `available_sources`.

Reuses `test_refunds.py`'s own fixtures (`refund_application`,
`paid_refund_invoice`, `rf01`, `_request_refund`) rather than rebuilding an
approved-and-paid application from scratch — the same cross-file reuse
`test_models_split.py`/`test_refund_sweep.py` already do for `rf01` alone
(mirrors the lesson: a conftest needs plumbing copied from an existing one,
not just fixtures, and here the existing FILE already is that plumbing)."""

import uuid

from app.modules.applications.models import Application
from app.modules.payments.models import Invoice
from tests.modules.applications.conftest import published_contour as published_contour
from tests.modules.gis.conftest import approval_doc as approval_doc
from tests.modules.gis.conftest import contours_layer as contours_layer
from tests.modules.gis.conftest import leshoz as leshoz
from tests.modules.payments.conftest import BUDGET_RECIPIENT_ID
from tests.modules.payments.test_refunds import REFUNDS, _request_refund
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
    components = response.json()["components"]
    assert sorted(c["amount"] for c in components) == ["100000.00", "500000.00"]
    assert all(c["account"] is None for c in components)
    budget_component = next(c for c in components if c["recipient_id"] == str(BUDGET_RECIPIENT_ID))
    assert budget_component["name"]["uz_latn"] == "Davlat byudjeti"
