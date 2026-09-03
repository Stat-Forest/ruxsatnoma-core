"""Wire schemas for the accountant's back office (plan
`03.10b-payments-reconciliation`). Separate from `schemas.py`, which is the
applicant-facing invoice surface: these are read by exactly one audience —
`payments.view`/`payments.manage` holders — and the two files' futures diverge
(refunds, manual confirmations and the discrepancy register all land here).

Money is carried on the wire as a STRING, never a JSON float — the same
fixed-scale-NUMERIC rule `schemas.InvoiceOut.amount` follows and for the same
reason: 2 060 000.00 has no exact binary representation and a tiyin lost on the
wire is a tiyin lost in a financial ledger.

Task 9 adds the refund schemas below. They are the one exception to "read by
exactly one audience": `RefundOut` is also what an applicant's own
`POST /refunds` (mounted at the ROOT `/refunds` prefix by `refunds_router.py`,
design/03 — never under `/payments`) answers with, since ruling 7 lets an
applicant file for their own application. `RefundOut.budget_account` is
`None` on every response before `approve` decides `returned`, and stays
`None` afterwards too when the resolution is `rejected` — see
`backoffice_service.approve_refund`'s own docstring for why the field is
present rather than omitted (`tz/12` #15, ruling 5).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class StatementAccepted(BaseModel):
    """`POST /payments/bank-statements` answers 202 with the id and the status
    it was queued in: the file is stored and QUEUED, and the parse happens in
    `jobs.process_bank_statements`. Poll `GET /payments/bank-statements/{id}`
    for the result."""

    id: uuid.UUID
    status: str


class StatementLineOut(BaseModel):
    """One imported row. `payer_account` is here because the accountant reads
    it, never because anything matches on it (`matcher.py`'s own docstring)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_no: int
    doc_number: str | None
    amount: Decimal
    operation_date: date
    payer_name: str | None
    payer_account: str | None
    purpose: str | None
    match_status: str
    matched_invoice_id: uuid.UUID | None

    @field_serializer("amount")
    def _amount(self, value: Decimal) -> str:
        return str(value)


class StatementOut(BaseModel):
    """`GET /payments/bank-statements/{id}` — the header plus a page of its
    lines. `stats` counts what was imported, skipped and how each line was
    classified; `error_report` carries the rows the parser refused. The two are
    NOT mutually exclusive here (unlike `gis.schemas.ImportOut`): one bad row
    out of three hundred is a warning, so a `parsed` statement can carry both."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    format: str
    file_id: uuid.UUID | None
    statement_date: date
    period_from: date | None
    period_to: date | None
    column_map: dict[str, Any]
    status: str
    stats: dict[str, Any]
    error_report: dict[str, Any] | None
    created_at: datetime
    lines: list[StatementLineOut]
    lines_total: int


class ReconciliationOut(BaseModel):
    """One row of the discrepancy register (`GET /payments/reconciliations`) —
    either a per-line comparison (`statement_line_id` set) or a whole
    statement's provider-settlement period row (`statement_line_id` and
    `transaction_id` both `None`, both totals named in `comment` —
    `statement_service._period_reconciliation`)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    statement_line_id: uuid.UUID | None
    transaction_id: uuid.UUID | None
    invoice_id: uuid.UUID | None
    result: str
    difference: Decimal | None
    status: str
    assigned_to: uuid.UUID | None
    comment: str | None
    resolution_doc_id: uuid.UUID | None
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    occurred_at: datetime

    @field_serializer("difference")
    def _difference(self, value: Decimal | None) -> str | None:
        return None if value is None else str(value)


class ReconciliationResolveIn(BaseModel):
    """`POST /payments/reconciliations/{id}/resolve` — `tz/08`: close a
    discrepancy with a comment or with a correcting document.

    `comment` is required by the SCHEMA (its absence is `ERR-VAL-001` from
    FastAPI's own validation, before the service ever runs); a comment that
    is present but blank (`""`, `"   "`) is a service-level check instead
    (`backoffice_service.resolve_reconciliation`), because a Pydantic length
    check cannot see past whitespace the way `str.strip()` can."""

    comment: str
    resolution_doc_id: uuid.UUID | None = None


class ManualConfirmationIn(BaseModel):
    """`POST /payments/manual-confirmations` — `tz/08` §4's one exception to
    `tz/05` invariant 3.

    `bank_doc_file_id` is REQUIRED and has no default: ruling 1 makes the
    stored bank document what makes the exception legal, so an omitted one is
    `ERR-VAL-001` from FastAPI's own validation, before the service runs —
    the same unrepresentability `manual_payment_confirmations.bank_doc_file_id`
    enforces as a NOT NULL FK.

    `amount` is the amount the BANK DOCUMENT says arrived, which may
    legitimately disagree with `invoices.amount` (ruling 5): an underpayment
    is a real thing an accountant confirms and then reconciles. It is
    accepted, recorded and flagged — never refused.

    **`gt=0` is the guard this door removed from the money path and has to
    put back.** On the Payme side `CreateTransaction`/`CheckPerformTransaction`
    pin the amount to `invoice.amount` and refuse a mismatch with `-31001`;
    here nothing does, and an unbounded `Decimal` was driven end to end
    against a real 150 000,00 invoice: `-2060000.00` filed 201, confirmed
    200, marked the invoice `paid` and the application `PAID`, and wrote two
    NEGATIVE `allocations` rows. `0.00` settled the invoice in full with a
    zero ledger. Ruling 5 accepts an UNDERPAYMENT — money that arrived, less
    than was owed — never a negative or a zero settlement, neither of which
    is a payment at all.

    **No business ceiling, deliberately.** An overpayment is a documented
    refund ground in `tz/08`, so capping the upper end would refuse a real
    case. `max_digits`/`decimal_places` mirror `numeric(18, 2)` exactly and
    exist only so an overflow is a 422 at the edge rather than a
    `DataError` 500 from the database."""

    invoice_id: uuid.UUID
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    paid_at: datetime
    bank_doc_file_id: uuid.UUID


class ManualConfirmationRejectIn(BaseModel):
    """`POST /payments/manual-confirmations/{id}/reject` — a rejection must
    say why (ruling 7). A missing field is FastAPI's own `ERR-VAL-001`; a
    present-but-blank one is the service's own check, since a Pydantic `str`
    requirement cannot see past whitespace the way `str.strip()` can (same
    split as `ReconciliationResolveIn.comment`)."""

    reason: str


class ManualConfirmationOut(BaseModel):
    """One `manual_payment_confirmations` row — the maker's filing, and what
    the checker's confirm/reject answers with.

    `bank_doc_file_id` is on the wire on purpose: the document is the whole
    legal basis of a manual PAID, so a checker asked to approve one must be
    able to reach it from this response alone."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_id: uuid.UUID
    amount: Decimal
    paid_at: datetime
    bank_doc_file_id: uuid.UUID
    maker_id: uuid.UUID
    checker_id: uuid.UUID | None
    status: str
    reason: str | None
    checked_at: datetime | None
    created_at: datetime

    @field_serializer("amount")
    def _amount(self, value: Decimal) -> str:
        return str(value)


class FiledManualConfirmationOut(ManualConfirmationOut):
    """The filing's own response, with the one fact that is NOT a column:
    whether the bank document's amount equals the invoice's (ruling 5).

    A subclass rather than a nullable field on the parent, because only the
    filing path reads the invoice to compare — `reject` never locks it, so a
    flag on every response would be a value the checker's own routes could
    not honestly fill in. `false` here always comes with an OPEN
    `reconciliations` row for the difference."""

    amount_matches_invoice: bool


# --- Task 9: refunds -----------------------------------------------------


class RefundRequestIn(BaseModel):
    """`POST /refunds` (design/03) — an applicant appealing their own
    application, or an accountant filing on anyone's behalf (the ownership
    rule lives in `backoffice_service._may_request_refund_for`, never here).

    `basis_item_id` names one of the four seeded `refund_reasons` items
    (`RF-01`..`RF-04`, migration `0022`) — checked as an ACTIVE classifier
    item by the service, not by this schema, the same existence-not-validity
    split `backoffice_service._assert_doc_active` already draws for a
    document id."""

    application_id: uuid.UUID
    basis_item_id: uuid.UUID
    comment: str | None = None


class RefundSubmitDecisionIn(BaseModel):
    """`POST /refunds/{id}/submit-decision` — the accountant's (`payments.
    manage`) own half (ruling 4): the three-way breakdown and the amount it
    is meant to sum to. Checked against `refunds.breakdown_is_complete` in
    code BEFORE the insert, so a mismatch answers `ERR-VAL-001` rather than
    an IntegrityError 500 from `returned_needs_complete_breakdown` — even
    though that CHECK only fires once `approve` moves the row to `returned`,
    catching the arithmetic here is what keeps a wrong number from ever
    reaching the rahbar's screen at all.

    Each component defaults to `0.00`, not `None`: an accountant who leaves
    a source untouched means "nothing from here", the same reading
    `refunds.breakdown_is_complete`'s own `coalesce`-style treatment of
    `None` already gives it — a bare `Field(ge=0, ...)` on each keeps a
    negative component (which `returned_needs_complete_breakdown` does not
    itself forbid) out of a financial ledger at the edge, before it becomes
    a negative-of-a-negative allocation."""

    final_amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    budget_amount: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=18, decimal_places=2)
    recipient_amount: Decimal = Field(
        default=Decimal("0.00"), ge=0, max_digits=18, decimal_places=2
    )
    other_amount: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=18, decimal_places=2)
    comment: str | None = None


class RefundApproveIn(BaseModel):
    """`POST /refunds/{id}/approve` — the rahbar's (`payments.confirm`) own
    half: `resolution="returned"` validates the breakdown again (defensive —
    see `approve_refund`'s own docstring), writes the negative ledger
    entries and moves the refund to `returned`; `resolution="rejected"`
    moves it to `rejected` and writes nothing to `allocations` — no money
    ever moved, so there is nothing to reverse (design/03: "approval →
    status returned/rejected")."""

    resolution: str = Field(pattern="^(returned|rejected)$")
    comment: str | None = None


class RefundAllocationOut(BaseModel):
    """One ledger row `approve_refund` just wrote — what makes ruling 5's
    NULL visible on the wire rather than only in the database (see
    `RefundOut.budget_account`'s own docstring)."""

    model_config = ConfigDict(from_attributes=True)

    target: str
    account: str | None
    amount: Decimal

    @field_serializer("amount")
    def _amount(self, value: Decimal) -> str:
        return str(value)


class RefundOut(BaseModel):
    """One `refunds` row. `suggested_amount`/`suggestion_reason` are the
    formula's hint (`backoffice_service.request_refund`'s own docstring
    lists every degenerate case that leaves `suggested_amount` `None`); a
    hint is never an error, so `POST /refunds` always answers 201 with one
    of the two set.

    `budget_account`/`recipient_account` are NOT columns on `refunds` — they
    are filled in only by `POST /refunds/{id}/approve`'s own response, from
    the allocations that call just wrote, and stay `None` on every other
    response (nothing has been decided yet to have an account at all).
    **`budget_account` is `None` by design, not by omission** (`tz/12` #15
    — the state budget's account number is stored nowhere in this system):
    the field is declared here, with an explicit default, specifically so an
    accountant reading this response sees a `null` the API chose to report
    rather than a key that silently is not there. `allocations` carries the
    same two rows in full (target, account, amount) for a client that wants
    more than the two named accounts."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    application_id: uuid.UUID
    invoice_id: uuid.UUID
    basis_item_id: uuid.UUID
    suggested_amount: Decimal | None
    suggestion_reason: str | None
    final_amount: Decimal | None
    budget_amount: Decimal | None
    recipient_amount: Decimal | None
    other_amount: Decimal | None
    status: str
    requested_by: uuid.UUID | None
    requested_at: datetime
    due_at: date
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    comment: str | None
    recipient_account: str | None = None
    budget_account: str | None = None
    allocations: list[RefundAllocationOut] = Field(default_factory=list)

    @field_serializer(
        "suggested_amount", "final_amount", "budget_amount", "recipient_amount", "other_amount"
    )
    def _money(self, value: Decimal | None) -> str | None:
        return None if value is None else str(value)
