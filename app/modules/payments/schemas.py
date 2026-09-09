"""Wire schema for reading `invoices` (`GET /invoices/{id}`, `GET
/invoices?application_id=`) and for starting a payment (`POST
/invoices/{id}/pay-intents`, task 5). No write schema for the invoice
itself: issuing and cancelling one are event-driven (`subscribers.py`),
never a direct client action.

Stage 7.9 task 8 adds `InvoiceOut.recipients` (`InvoiceRecipientOut` below)
— the invoice's own frozen split, decision #154. It is gated on
`payments.view` at the ROUTER (`router.py::get_invoice`), never here: a
citizen paying their own invoice must see the total, never who receives
it (Override 4 — internal allocation is not part of what they are paying
for)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from app.core.schemas import LocalizedName


class InvoiceRecipientOut(BaseModel):
    """One row of an invoice's split, FROZEN at issuance
    (`payments.models.InvoiceRecipient`, decision #158) — `InvoiceOut.
    recipients`, ordered by `position`, the LAST row always the leshoz's
    own remainder (`kind="remainder"`, `recipient_id=None`).

    Carries the full frozen rule, not just the resulting `amount`: `kind`/
    `percent`/`fixed_amount` are what a `payments.view` holder needs to see
    WHY a share is what it is, the same fields `PaymentRecipientOut`
    exposes for the live directory this snapshot was copied from."""

    model_config = ConfigDict(from_attributes=True)

    recipient_id: uuid.UUID | None
    name: dict[str, Any]
    payme_account_id: str | None
    kind: str
    percent: Decimal | None
    fixed_amount: Decimal | None
    amount: Decimal

    # Same fixed-scale-NUMERIC lesson as `InvoiceOut.amount` below.
    @field_serializer("amount")
    def _amount(self, value: Decimal) -> str:
        return str(value)

    @field_serializer("percent", "fixed_amount")
    def _money(self, value: Decimal | None) -> str | None:
        return None if value is None else str(value)


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str
    application_id: uuid.UUID
    calculation_id: uuid.UUID | None
    amount: Decimal
    status: str
    issued_at: datetime
    due_at: datetime
    paid_at: datetime | None
    # Populated by the router ONLY for a `payments.view` holder (Override 4)
    # and OMITTED from the JSON body — never sent as `null` — for anyone
    # else (`router.py`'s own `_invoice_out`: `exclude={"recipients"}`, not
    # a blanket `exclude_none`, since `AllocationOut.account` elsewhere in
    # this module documents the opposite convention for its OWN `None` — a
    # null there must stay visible, so this field alone disappears, not
    # every other one). Absent on `GET /invoices` (the list route) too,
    # the same scope `RefundOut.available_sources` already draws for
    # itself: "a real absence, not a hidden default."
    recipients: list[InvoiceRecipientOut] | None = None

    # Same fixed-scale-NUMERIC lesson as `norms.schemas.TariffOut.coefficient`:
    # money is carried on the wire as a string, never a JSON float.
    @field_serializer("amount")
    def _amount(self, value: Decimal) -> str:
        return str(value)


class PayIntentIn(BaseModel):
    """`{provider: "payme"}` (design/03 §payments) — `Literal` rather than a
    service-level check against `payments.models.PAYMENT_PROVIDERS`: the
    checkout-redirect this route builds is Payme-specific (§3.8), and
    "manual" goes through `manual_payment_confirmations` (3.10b, not built
    yet), never through this route."""

    provider: Literal["payme"]


class PayIntentOut(BaseModel):
    payment_url: str


# --- Stage 7.9 (recipients directory, task 3) --------------------------------
#
# `kind` is spelled out by hand as `Literal["percent", "fixed"]`, never
# `Literal[*RECIPIENT_KINDS]` (lesson: "An enum-ish column has ONE source of
# truth: the tuple" — `Literal[*TUPLE]` runs, but pyright's
# `reportInvalidTypeForm` rejects a `Literal` whose members are not statically
# visible). `tests/modules/payments/test_recipients_api.py`'s own consistency
# test closes the gap: `set(get_args(RecipientKind)) == set(RECIPIENT_KINDS)`.
RecipientKind = Literal["percent", "fixed"]


class PaymentRecipientIn(BaseModel):
    """`POST /payments/recipients` (decisions #154, #157). Exactly ONE of
    `percent`/`fixed_amount` may be set, matching `kind` — the DB CHECK
    `rule_matches_kind` (migration `0045`) enforces the same rule, but a
    client sending the wrong one should see a 422 here, never an
    `IntegrityError` turned 500 (lesson: "A `response_model` mismatch is
    invisible to ruff and pyright" sits beside this one — the schema is what
    turns a database constraint into a client-facing error).

    `active` is deliberately absent: every new row starts active (the
    model's own default), and a row is deactivated afterwards through
    `PATCH`, never created inactive — decision #157 makes deactivation, not
    creation, the point where a row stops counting."""

    name: LocalizedName
    payme_account_id: str | None = None
    kind: RecipientKind
    percent: Decimal | None = Field(default=None, gt=0, le=100, decimal_places=2)
    fixed_amount: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    sort_order: int = 0
    note: str | None = None

    @model_validator(mode="after")
    def _one_rule_only(self) -> Self:
        if self.kind == "percent" and (self.percent is None or self.fixed_amount is not None):
            raise ValueError("a percent recipient carries percent and no fixed_amount")
        if self.kind == "fixed" and (self.fixed_amount is None or self.percent is not None):
            raise ValueError("a fixed recipient carries fixed_amount and no percent")
        return self


class PaymentRecipientPatch(BaseModel):
    """`PATCH /payments/recipients/{id}` — every field optional, only the
    keys actually sent are touched (`exclude_unset=True`, the convention
    `LegalDocumentPatchIn` established). `kind` is absent: it never changes
    after creation, so `percent`/`fixed_amount` here always mean "the row's
    OWN kind's own amount" — `recipients_service.update` refuses whichever
    one does not match the row's `kind`, the same reasoning `_one_rule_only`
    above enforces at creation."""

    name: LocalizedName | None = None
    payme_account_id: str | None = None
    percent: Decimal | None = Field(default=None, gt=0, le=100, decimal_places=2)
    fixed_amount: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    sort_order: int | None = None
    note: str | None = None
    active: bool | None = None


class PaymentRecipientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: dict[str, Any]
    payme_account_id: str | None
    kind: str
    percent: Decimal | None
    fixed_amount: Decimal | None
    active: bool
    sort_order: int
    note: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    # Same fixed-scale-NUMERIC lesson as `InvoiceOut.amount` above: a
    # `Numeric` column is a string on the wire, never a JSON float — and
    # `None` must serialize to `null`, not the string `"None"`.
    @field_serializer("percent", "fixed_amount")
    def _money(self, value: Decimal | None) -> str | None:
        return None if value is None else str(value)
