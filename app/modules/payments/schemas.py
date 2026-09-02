"""Wire schema for reading `invoices` (`GET /invoices/{id}`, `GET
/invoices?application_id=`). No write schema in this task: issuing and
cancelling an invoice are event-driven (`subscribers.py`), never a direct
client action — Task 3+ adds the provider-facing write surface."""

import uuid
from datetime import datetime
from decimal import Decimal

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
