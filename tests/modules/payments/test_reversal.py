"""The post-perform reversal recorder (ruling 15, plan
`03.10b-payments-reconciliation` task 8) — what 3.10a named as its own KNOWN
GAP and left open.

Payme's `CancelTransaction` on an ALREADY-PERFORMED transaction (state `2` ->
`-2`; `design/04` §3.5 reason `5` is literally "funds returned") recorded the
reversal on `provider_transactions` and NOTHING else. This file pins what
3.10b writes instead: two `correction` allocations negating what that
transaction wrote (which brings this invoice's whole ledger back to `0.00`,
since only one transaction can perform against it — see
`test_a_post_perform_cancel_brings_the_ledger_back_to_zero`), one open
`reconciliations` row whose `difference` is the money that went back, an RI-01
audit row, and — only when a permit in a LIVE status already exists for that
application — a second audit row carrying RI-10. A `revoked` permit does not
raise it: an operator has already dealt with that one.

And it pins, just as hard, the two things this stage does NOT do: the invoice
stays `paid` and the application stays `PAID`
(`test_the_invoice_stays_paid_and_the_application_stays_paid`), and a
state-`1` cancellation still writes none of the above.

**Every test drives `payme.handle` directly, not the HTTP route** — unlike
`test_payme_rpc.py`, which must go over HTTP because the always-200 guarantee
is a property of `payme_router.py`. Nothing here is about HTTP, and the
difference matters for the shared test database: the HTTP `client` fixture
commits every request for real (`_commit_pending_before_requests`), which
would commit this file's own contour/permit scaffolding too. Driven through
`db` alone, every row this file writes is rolled back with the test.

**The permit rows are built directly, with raw SQL** (ruling 20): `payments` and
`permits` are both level 4 (`design/01` rule 3), and a payments test that
imported `permits` would be the first crack in the boundary the implementation
itself is careful not to cross. The assertions are on `audit_log`, never on the
permit.

Every Payme transaction id is generated per run (lesson: "the test DB is
shared, persistent, and never empty"), and every query below is scoped to the
invoice or application this test itself created — an unscoped `count(*)` over
`allocations`/`reconciliations` counts every other test's committed rows too.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications import service as applications_service
from app.modules.applications.models import Application
from app.modules.audit.models import AuditLog
from app.modules.auth.models import User
from app.modules.gis.models import GisLayer
from app.modules.notifications.models import Notification
from app.modules.payments import payme
from app.modules.payments import service as payments_service
from app.modules.payments.models import (
    Allocation,
    Invoice,
    ProviderTransaction,
    Reconciliation,
)
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import contours_layer as contours_layer
from tests.modules.gis.conftest import leshoz as leshoz
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt

RI_UNCONFIRMED_PAID = "RI-01"
RI_PERMIT_WITHOUT_PAYMENT = "RI-10"


def _tx_id(label: str) -> str:
    return f"payme-{label}-{uuid.uuid4().hex[:12]}"


async def _create(db: AsyncSession, invoice: Invoice, label: str) -> tuple[str, datetime]:
    """A Payme transaction in state `1` against `invoice`."""
    now = datetime.now(UTC)
    tx_id = _tx_id(label)
    await payme.handle(
        db,
        "CreateTransaction",
        {
            "id": tx_id,
            "time": int(now.timestamp() * 1000),
            "amount": int(invoice.amount * 100),
            "account": {"id": invoice.number},
        },
        now=now,
    )
    return tx_id, now


async def _perform(db: AsyncSession, invoice: Invoice, label: str) -> tuple[str, datetime]:
    """A Payme transaction in state `2` — money confirmed, ledger written."""
    tx_id, now = await _create(db, invoice, label)
    await payme.handle(db, "PerformTransaction", {"id": tx_id}, now=now)
    return tx_id, now


async def _cancel(db: AsyncSession, tx_id: str, now: datetime, *, reason: int = 5) -> dict:
    return await payme.handle(db, "CancelTransaction", {"id": tx_id, "reason": reason}, now=now)


async def _allocations(db: AsyncSession, invoice: Invoice) -> list[Allocation]:
    stmt = select(Allocation).where(Allocation.invoice_id == invoice.id)
    return list((await db.execute(stmt)).scalars().all())


async def _reconciliations(db: AsyncSession, invoice: Invoice) -> list[Reconciliation]:
    stmt = select(Reconciliation).where(Reconciliation.invoice_id == invoice.id)
    return list((await db.execute(stmt)).scalars().all())


async def _risk_rows(db: AsyncSession, indicator: str, object_id: uuid.UUID) -> list[AuditLog]:
    stmt = select(AuditLog).where(
        AuditLog.object_id == object_id,
        AuditLog.extra["risk_indicator"].astext == indicator,
    )
    return list((await db.execute(stmt)).scalars().all())


async def _reversal_notifications(db: AsyncSession, invoice: Invoice) -> list[Notification]:
    stmt = select(Notification).where(
        Notification.object_id == invoice.id,
        Notification.event_code == "payment.reversed",
    )
    return list((await db.execute(stmt)).scalars().all())


async def _insert_permit(
    db: AsyncSession,
    *,
    application: Application,
    activity_type_id: uuid.UUID,
    leshoz,
    contours_layer: GisLayer,
    status: str,
) -> uuid.UUID:
    """A `permits` row for `application`, inserted with raw SQL — no `permits`
    import anywhere in this file (ruling 20; both modules are level 4).

    The contour sits at a random location (`random_box_wkt`) and its version
    stays a draft: nothing here is read by a geometric predicate, the permit
    only needs the two FK targets to exist.
    """
    contour = await make_contour(db, contours_layer, leshoz)
    version = await make_version(db, contour.id, random_box_wkt())
    permit_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO permits (id, series, number, application_id, applicant_id,"
            " activity_type_id, organization_id, contour_id, contour_version_id, area_ha,"
            " period_from, period_to, amount, status, qr_token, snapshot)"
            " VALUES (:id, :series, :number, :application_id, :applicant_id,"
            " :activity_type_id, :organization_id, :contour_id, :contour_version_id, 1.0000,"
            " :period_from, :period_to, 100.00, :status, :qr_token, '{}'::jsonb)"
        ),
        {
            "id": permit_id,
            "series": "AA",
            # uq_permits_series_number is (series, number) and the test DB is
            # shared: a literal would collide with a past run's committed row.
            "number": uuid.uuid4().int % 1_000_000_000 + 1,
            "application_id": application.id,
            "applicant_id": application.applicant_id,
            "activity_type_id": activity_type_id,
            "organization_id": leshoz.id,
            "contour_id": contour.id,
            "contour_version_id": version.id,
            "period_from": date(2027, 1, 1),
            "period_to": date(2027, 12, 31),
            "status": status,
            "qr_token": uuid.uuid4().hex,
        },
    )
    return permit_id


@pytest.fixture
async def active_permit(
    db: AsyncSession,
    pending_invoice: Invoice,
    approved_application: Application,
    grazing_activity_id: uuid.UUID,
    leshoz,
    contours_layer: GisLayer,
) -> uuid.UUID:
    """An `active` permit for the same application. `pending_invoice` is
    depended on FIRST so its own `db.commit()` runs before any of this
    fixture's rows exist: everything below is then flushed only, and rolls back
    with the test rather than leaving a stray contour in the shared database."""
    return await _insert_permit(
        db,
        application=approved_application,
        activity_type_id=grazing_activity_id,
        leshoz=leshoz,
        contours_layer=contours_layer,
        status="active",
    )


@pytest.fixture
async def revoked_permit(
    db: AsyncSession,
    pending_invoice: Invoice,
    approved_application: Application,
    grazing_activity_id: uuid.UUID,
    leshoz,
    contours_layer: GisLayer,
) -> uuid.UUID:
    """The same row in `revoked` — the status whose RI-10 was a false positive
    until fix round 1 (see `test_no_ri_10_for_a_revoked_permit`)."""
    return await _insert_permit(
        db,
        application=approved_application,
        activity_type_id=grazing_activity_id,
        leshoz=leshoz,
        contours_layer=contours_layer,
        status="revoked",
    )


@pytest.fixture
async def leshoz_head(db: AsyncSession, leshoz) -> User:
    """`executor_head` of `leshoz` — `permits.manage`'s own holder, and ruling
    #112's notification recipient once a reversal's RI-10 fires under an
    active permit of this organization."""
    return await make_user(db, role_code="executor_head", organization_id=leshoz.id)


async def test_a_post_perform_cancel_brings_the_ledger_back_to_zero(
    db: AsyncSession, pending_invoice: Invoice
):
    """Two `correction` rows negating the two `payment` rows — the ledger is
    append-only (ruling 4), so a reversal is new rows, never an update of the
    old ones.

    The whole-invoice sum is `0.00` HERE because exactly one transaction ever
    performed against this invoice, which is all today's paths allow
    (`payme._perform_transaction` refuses an invoice that is not `pending`, and
    a reversal leaves it `paid`). That is the property under test, not a
    universal law: `record_reversal` negates the rows of the transaction that
    was cancelled, so on an invoice with two performing transactions its own
    entries would still cancel out while the invoice's total would not be zero
    — and that would be correct (fix round 1)."""
    tx_id, now = await _perform(db, pending_invoice, "ledger")

    before = await _allocations(db, pending_invoice)
    assert [row.entry_type for row in before] == ["payment", "payment"]

    await _cancel(db, tx_id, now)

    after = await _allocations(db, pending_invoice)
    corrections = [row for row in after if row.entry_type == "correction"]
    assert len(after) == 4
    assert len(corrections) == 2
    # Stage 7.9 task 5: the seeded `budget_50` directory row (migration
    # `0045`) is now a configured RECEIVER, not the old engine's fixed
    # "budget" half — `record_reversal` negates whatever targets
    # `confirm_payment` actually wrote.
    assert {row.target for row in corrections} == {"recipient", "receiver"}
    # BLOCKER 1 (whole-branch review): `record_reversal` used to copy `target`
    # and `account` onto the correction row but drop `recipient_id`, so a
    # correction against the configured receiver's own row landed with
    # `target='receiver', recipient_id=None` — matching the target filter but
    # not `dashboard.repo`'s per-receiver sum, which keys on `recipient_id`.
    # The receiver's correction must carry the SAME `recipient_id` its
    # positive twin did, not `None`.
    before_receiver = next(row for row in before if row.target == "receiver")
    after_receiver_correction = next(row for row in corrections if row.target == "receiver")
    assert before_receiver.recipient_id is not None
    assert after_receiver_correction.recipient_id == before_receiver.recipient_id
    before_recipient = next(row for row in before if row.target == "recipient")
    after_recipient_correction = next(row for row in corrections if row.target == "recipient")
    assert after_recipient_correction.recipient_id == before_recipient.recipient_id is None
    assert all(row.amount < 0 for row in corrections)
    assert sum((row.amount for row in after), Decimal("0.00")) == Decimal("0.00")
    # Every correction traces back to the transaction that was reversed, and
    # names the Payme reason — the only place the reason survives outside
    # `provider_transactions`.
    assert all(row.transaction_id is not None for row in corrections)
    # The whole suffix, not a bare "5" — the transaction's own random hex id is
    # in the same string and would satisfy a substring check on the digit alone
    # even with the reason dropped entirely (fix round 1).
    assert all((row.note or "").endswith("(cancel reason 5)") for row in corrections)


async def test_a_post_perform_cancel_opens_one_discrepancy_row(
    db: AsyncSession, pending_invoice: Invoice
):
    """The register row is the operator's handle (ruling 15): one
    `reconciliations` row, `result='discrepancy'`, `status='open'`, whose
    `difference` is the money that went back."""
    tx_id, now = await _perform(db, pending_invoice, "register")
    assert await _reconciliations(db, pending_invoice) == []

    await _cancel(db, tx_id, now)

    rows = await _reconciliations(db, pending_invoice)
    assert len(rows) == 1
    assert rows[0].result == "discrepancy"
    assert rows[0].status == "open"
    assert rows[0].difference == pending_invoice.amount
    assert rows[0].transaction_id is not None


async def test_a_post_perform_cancel_writes_an_ri_01_audit_row(
    db: AsyncSession, pending_invoice: Invoice
):
    """`tz/10` RI-01 — «PAID без подтверждения провайдера/банка». The provider
    has withdrawn its confirmation and the invoice is still `paid`, which is
    exactly what that indicator describes."""
    tx_id, now = await _perform(db, pending_invoice, "ri01")
    assert await _risk_rows(db, RI_UNCONFIRMED_PAID, pending_invoice.id) == []

    await _cancel(db, tx_id, now)

    rows = await _risk_rows(db, RI_UNCONFIRMED_PAID, pending_invoice.id)
    assert len(rows) == 1
    assert rows[0].object_type == "invoice"
    assert rows[0].result == "success"
    assert rows[0].new_value is not None
    assert rows[0].new_value["reason"] == 5


async def test_ri_10_fires_only_when_a_permit_already_exists(
    db: AsyncSession, pending_invoice: Invoice, active_permit: uuid.UUID
):
    """`tz/10` RI-10 — «Разрешение активировано без оплаты», critical and
    immediate. A reversed payment under an existing permit IS that, and it is
    the case ruling 15 leaves an operator to resolve by hand (3.11b's revoke).

    Asserted on `audit_log`, never on the permit row: `payments` may not read
    `permits` as a module at all."""
    tx_id, now = await _perform(db, pending_invoice, "ri10")

    await _cancel(db, tx_id, now)

    rows = await _risk_rows(db, RI_PERMIT_WITHOUT_PAYMENT, pending_invoice.application_id)
    assert len(rows) == 1
    assert rows[0].object_type == "application"


async def test_ri_10_also_notifies_the_leshoz_head(
    db: AsyncSession,
    pending_invoice: Invoice,
    active_permit: uuid.UUID,
    leshoz_head: User,
):
    """Ruling #112: raising RI-10 is not enough on its own — the whole point
    is that a HUMAN at the leshoz decides whether to suspend, and nobody
    decides on a row nobody was told about. `leshoz_head` holds
    `permits.manage` on the SAME organization `_insert_permit` gave the
    permit, so `record_reversal` must find and notify exactly them."""
    tx_id, now = await _perform(db, pending_invoice, "notify")

    await _cancel(db, tx_id, now)

    rows = await _reversal_notifications(db, pending_invoice)
    assert len(rows) == 1
    assert rows[0].recipient_user_id == leshoz_head.id
    assert rows[0].object_type == "invoice"
    assert rows[0].params["invoice_number"] == pending_invoice.number


async def test_no_ri_10_for_a_revoked_permit(
    db: AsyncSession, pending_invoice: Invoice, revoked_permit: uuid.UUID
):
    """A `revoked` permit is one an operator has ALREADY dealt with, so a
    reversal under it is not «Разрешение активировано без оплаты» — and RI-10
    is CRITICAL in `tz/10` and harvested by string by 4.2, so a false positive
    there is expensive.

    Until fix round 1 the count behind this had no status filter and every
    permit row counted the same, `revoked` and `expired` included. The RI-01
    row still fires: the money did come back."""
    tx_id, now = await _perform(db, pending_invoice, "revoked")

    await _cancel(db, tx_id, now)

    assert await _risk_rows(db, RI_PERMIT_WITHOUT_PAYMENT, pending_invoice.application_id) == []
    assert len(await _risk_rows(db, RI_UNCONFIRMED_PAID, pending_invoice.id)) == 1


async def test_recording_the_same_reversal_twice_records_it_once(
    db: AsyncSession, pending_invoice: Invoice, active_permit: uuid.UUID
):
    """`record_reversal`'s own guard: a `correction` row already standing
    against this transaction means it ran before, and a second pass would
    negate the ledger a second time — silently, and with no way to tell the
    duplicate rows from the real ones afterwards.

    `payme`'s state machine cannot produce a second call (state `-2` is
    terminal per transaction, and the branch that calls this is only reached
    from state `2`), so the guard exists for a manual re-run — which is exactly
    why it needs a test of its own: nothing else would notice if a refactor
    dropped it."""
    tx_id, now = await _perform(db, pending_invoice, "twice")
    await _cancel(db, tx_id, now)

    after_first = await _allocations(db, pending_invoice)
    transaction = (
        await db.execute(
            select(ProviderTransaction).where(ProviderTransaction.external_id == tx_id)
        )
    ).scalar_one()

    await payments_service.record_reversal(
        db, invoice=pending_invoice, transaction=transaction, reason=5
    )

    assert len(await _allocations(db, pending_invoice)) == len(after_first)
    assert len(await _reconciliations(db, pending_invoice)) == 1
    assert len(await _risk_rows(db, RI_UNCONFIRMED_PAID, pending_invoice.id)) == 1
    assert len(await _risk_rows(db, RI_PERMIT_WITHOUT_PAYMENT, pending_invoice.application_id)) == 1


async def test_no_ri_10_when_no_permit_exists(db: AsyncSession, pending_invoice: Invoice):
    """The mirror of the test above — without the permit fixture, the second
    audit row must not appear. The count on `permits` is a real check, not a
    line that always fires."""
    tx_id, now = await _perform(db, pending_invoice, "no-ri10")

    await _cancel(db, tx_id, now)

    assert await _risk_rows(db, RI_PERMIT_WITHOUT_PAYMENT, pending_invoice.application_id) == []


async def test_no_notification_when_no_permit_exists(db: AsyncSession, pending_invoice: Invoice):
    """The notify half mirrors the indicator half exactly: with no permit at
    all there is no `executor_head` decision to make yet, so `record_reversal`
    must raise RI-01 alone and tell nobody."""
    tx_id, now = await _perform(db, pending_invoice, "no-notify")

    await _cancel(db, tx_id, now)

    assert await _risk_rows(db, RI_UNCONFIRMED_PAID, pending_invoice.id) != []
    assert await _reversal_notifications(db, pending_invoice) == []


async def test_the_invoice_stays_paid_and_the_application_stays_paid(
    db: AsyncSession, pending_invoice: Invoice
):
    """THIS TEST IS THE RULING (15), pinned so a later reader cannot mistake it
    for an oversight.

    A post-perform reversal is RECORDED, never propagated. The application is
    NOT moved off PAID, because it cannot be:
    `applications.service.APPLICATION_TRANSITIONS["PAID"]` is
    `frozenset({"PERMIT_ISSUED"})` and `tz/05` gives PAID no other exit.
    Widening it is a change to the application state machine — a `tz/05`
    change owned by stage 3.9, with 3.11's issuance gate and 3.11b's revoke
    both reading it — and doing it from a `payments` branch would be a silent
    cross-module break, not a fix. The invoice is left `paid` for the same
    reason: `design/02` says the invoice status is not rewritten and the
    history stays intact.

    The residual window this leaves — a permit can still be issued between the
    reversal and an operator acting on the register row — is stated in
    `payments/service.py`'s public-surface banner and filed for the Agency in
    `tz/12`. It is not closed here."""
    tx_id, now = await _perform(db, pending_invoice, "statuses")
    await db.refresh(pending_invoice)
    assert pending_invoice.status == "paid"

    await _cancel(db, tx_id, now)

    await db.refresh(pending_invoice)
    assert pending_invoice.status == "paid"
    application = await applications_service.get(db, pending_invoice.application_id)
    assert application is not None
    assert application.status == "PAID"
    assert applications_service.APPLICATION_TRANSITIONS["PAID"] == frozenset({"PERMIT_ISSUED"})


async def test_cancelling_a_never_performed_transaction_records_nothing(
    db: AsyncSession, pending_invoice: Invoice, active_permit: uuid.UUID
):
    """3.10a's behaviour for state `1` -> `-1`, unchanged: no money ever
    arrived, so there is nothing to reverse — no ledger row, no register row,
    neither risk indicator, and (ruling #112) no notification either. The
    permit fixture is present on purpose: even then, a transaction that never
    performed writes none of it."""
    tx_id, now = await _create(db, pending_invoice, "state-one")

    result = await _cancel(db, tx_id, now, reason=1)

    assert result["state"] == -1
    assert await _allocations(db, pending_invoice) == []
    assert await _reconciliations(db, pending_invoice) == []
    assert await _risk_rows(db, RI_UNCONFIRMED_PAID, pending_invoice.id) == []
    assert await _risk_rows(db, RI_PERMIT_WITHOUT_PAYMENT, pending_invoice.application_id) == []
    assert await _reversal_notifications(db, pending_invoice) == []
