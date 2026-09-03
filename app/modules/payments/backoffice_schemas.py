"""Wire schemas for the accountant's back office (plan
`03.10b-payments-reconciliation`). Separate from `schemas.py`, which is the
applicant-facing invoice surface: these are read by exactly one audience —
`payments.view`/`payments.manage` holders — and the two files' futures diverge
(refunds, manual confirmations and the discrepancy register all land here).

Money is carried on the wire as a STRING, never a JSON float — the same
fixed-scale-NUMERIC rule `schemas.InvoiceOut.amount` follows and for the same
reason: 2 060 000.00 has no exact binary representation and a tiyin lost on the
wire is a tiyin lost in a financial ledger.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer


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
