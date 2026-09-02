"""Wire schema for reading `invoices` (`GET /invoices/{id}`, `GET
/invoices?application_id=`) and for starting a payment (`POST
/invoices/{id}/pay-intents`, task 5). No write schema for the invoice
itself: issuing and cancelling one are event-driven (`subscribers.py`),
never a direct client action."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer


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
