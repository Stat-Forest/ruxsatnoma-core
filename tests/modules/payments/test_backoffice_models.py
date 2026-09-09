"""The five tables of stage 3.10b and the invariants the database itself
enforces (plan `03.10b-payments-reconciliation` task 1). Mirrors
`tests/modules/payments/test_models.py`'s own shape: no HTTP, no service —
pure ORM/CHECK-level tests against a real, migrated schema.

`users`/`media_file`/`bank_statement`/`refund_reason_item_id` are local
fixtures rather than additions to `conftest.py` (task 1's own file list does
not touch it): a genuine `media_files` row for `bank_doc_file_id` (a NOT NULL
FK), two DISTINCT users for maker/checker, and a real seeded
`classifier_items` row for `basis_item_id` — never the placeholder ids the
brief's own illustrative snippet admitted to borrowing from an unrelated
table.
"""

import hashlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Classifier, ClassifierItem
from app.modules.auth.models import User
from app.modules.payments.models import (
    BankStatement,
    BankStatementLine,
    ManualPaymentConfirmation,
    Reconciliation,
    Refund,
    RefundComponent,
)
from tests.modules.auth.test_sessions import make_user


@pytest.fixture
async def users(db: AsyncSession) -> tuple[User, User]:
    """Two DISTINCT staff users — maker and checker must never be the same
    person (`confirmed_needs_checker`)."""
    maker = await make_user(db)
    checker = await make_user(db)
    return maker, checker


@pytest.fixture
async def media_file(db: AsyncSession, users: tuple[User, User]):
    """A genuine `media_files` row — `bank_doc_file_id` is a NOT NULL FK, and
    a manual PAID without a real document is exactly what ruling 1 forbids."""
    from app.core.models import MediaFile

    maker, _checker = users
    row = MediaFile(
        storage_key=f"bank-docs/{uuid.uuid4().hex}.pdf",
        filename="bank-doc.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        sha256=hashlib.sha256(b"test bank document").hexdigest(),
        uploaded_by=maker.id,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def bank_statement(db: AsyncSession) -> BankStatement:
    row = BankStatement(source="file", format="csv", statement_date=date(2026, 9, 1))
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def refund_reason_item_id(db: AsyncSession) -> uuid.UUID:
    """A REAL seeded `classifier_items` row (migration `0022`'s
    `refund_reasons`), not a fabricated id — `basis_item_id` is a NOT NULL FK."""
    row_id = await db.scalar(
        select(ClassifierItem.id)
        .join(Classifier, ClassifierItem.classifier_id == Classifier.id)
        .where(Classifier.code == "refund_reasons", ClassifierItem.code == "RF-03")
    )
    assert row_id is not None, "migration 0022 must seed the refund_reasons classifier"
    return row_id


# --- manual_payment_confirmations -------------------------------------------


async def test_a_confirmed_manual_confirmation_needs_an_independent_checker(
    db: AsyncSession, invoice, media_file, users: tuple[User, User]
):
    """design/02's CHECK, at the database level — not in the service, so no
    future write path can go around it."""
    maker, _checker = users
    row = ManualPaymentConfirmation(
        invoice_id=invoice.id,
        amount=Decimal("2060000.00"),
        paid_at=datetime.now(UTC),
        bank_doc_file_id=media_file.id,
        maker_id=maker.id,
        checker_id=maker.id,
        status="confirmed",
    )
    db.add(row)
    with pytest.raises(IntegrityError, match="confirmed_needs_checker"):
        await db.flush()


async def test_a_confirmed_manual_confirmation_with_an_independent_checker_is_insertable(
    db: AsyncSession, invoice, media_file, users: tuple[User, User]
):
    maker, checker = users
    row = ManualPaymentConfirmation(
        invoice_id=invoice.id,
        amount=Decimal("2060000.00"),
        paid_at=datetime.now(UTC),
        bank_doc_file_id=media_file.id,
        maker_id=maker.id,
        checker_id=checker.id,
        status="confirmed",
    )
    db.add(row)
    await db.flush()
    assert row.id is not None


# --- bank_statements ---------------------------------------------------------


async def test_a_bank_statement_is_insertable(db: AsyncSession):
    row = BankStatement(source="file", format="csv", statement_date=date(2026, 9, 1))
    db.add(row)
    await db.flush()
    assert row.id is not None
    assert row.status == "pending"


async def test_a_bank_statement_rejects_an_unknown_source(db: AsyncSession):
    row = BankStatement(source="carrier_pigeon", format="csv", statement_date=date(2026, 9, 1))
    db.add(row)
    with pytest.raises(IntegrityError, match="source_valid"):
        await db.flush()


# --- bank_statement_lines -----------------------------------------------------


async def test_a_bank_statement_line_is_insertable(db: AsyncSession, bank_statement: BankStatement):
    row = BankStatementLine(
        statement_id=bank_statement.id,
        line_no=1,
        amount=Decimal("100.00"),
        operation_date=date(2026, 9, 1),
        raw={},
    )
    db.add(row)
    await db.flush()
    assert row.id is not None
    assert row.match_status == "unmatched"


async def test_a_bank_statement_line_rejects_an_unknown_match_status(
    db: AsyncSession, bank_statement: BankStatement
):
    row = BankStatementLine(
        statement_id=bank_statement.id,
        line_no=1,
        amount=Decimal("100.00"),
        operation_date=date(2026, 9, 1),
        raw={},
        match_status="lost_in_the_mail",
    )
    db.add(row)
    with pytest.raises(IntegrityError, match="match_status_valid"):
        await db.flush()


# --- reconciliations -----------------------------------------------------------


async def test_a_reconciliation_is_insertable(db: AsyncSession):
    row = Reconciliation(result="unknown")
    db.add(row)
    await db.flush()
    assert row.id is not None
    assert row.status == "open"


async def test_a_reconciliation_rejects_an_unknown_result(db: AsyncSession):
    row = Reconciliation(result="inconclusive")
    db.add(row)
    with pytest.raises(IntegrityError, match="result_valid"):
        await db.flush()


# --- refunds --------------------------------------------------------------------


async def test_a_returned_refund_needs_a_breakdown_that_sums_to_the_final_amount(
    db: AsyncSession, invoice, refund_reason_item_id: uuid.UUID
):
    """The invariant design/02 names moved from a row CHECK
    (`returned_needs_complete_breakdown`) to the `refund_components_complete`
    TRIGGER (decision #162, migration `0046`) — which fires on UPDATE only,
    never on INSERT, so this must reach `returned` through an update, the
    same way the real service does (`request_refund` always inserts
    `requested`, `approve_refund` is what moves a row to `returned`). A bare
    INSERT with `status="returned"` from the start would not exercise the
    trigger at all and would prove nothing."""
    row = Refund(
        application_id=invoice.application_id,
        invoice_id=invoice.id,
        basis_item_id=refund_reason_item_id,
        status="requested",
        requested_at=datetime.now(UTC),
        due_at=date(2026, 10, 1),
    )
    db.add(row)
    await db.flush()
    db.add(RefundComponent(refund_id=row.id, amount=Decimal("400000.00")))  # 400 000, not 1 000 000
    await db.flush()
    row.final_amount = Decimal("1000000.00")
    row.status = "returned"
    with pytest.raises(DBAPIError, match="do not sum to final_amount"):
        await db.flush()


async def test_a_returned_refund_with_a_matching_breakdown_is_insertable(
    db: AsyncSession, invoice, refund_reason_item_id: uuid.UUID
):
    row = Refund(
        application_id=invoice.application_id,
        invoice_id=invoice.id,
        basis_item_id=refund_reason_item_id,
        status="requested",
        requested_at=datetime.now(UTC),
        due_at=date(2026, 10, 1),
    )
    db.add(row)
    await db.flush()
    db.add(RefundComponent(refund_id=row.id, amount=Decimal("1000000.00")))
    await db.flush()
    row.final_amount = Decimal("1000000.00")
    row.status = "returned"
    await db.flush()
    assert row.id is not None
