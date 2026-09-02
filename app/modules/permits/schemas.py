"""API shapes for permits.

Two columns of `permits` are deliberately absent from every response here:

  * `qr_token` — a secret, not an identifier (ruling 8). It is the key to an
    unauthenticated page showing a citizen's name, leshoz and activity, so it is
    never returned in any list or card. The brief's own test asserts its absence.
  * `snapshot` — the frozen document body. It is what the PDF already says, and
    the permit card that renders it is Task 8's; putting a 20-key JSON blob on an
    issuance response would fix its shape here by accident.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer

# Spelled out rather than `Literal[*PERMIT_STATUSES]`: pyright rejects a starred
# variable inside `Literal` (`reportInvalidTypeForm`), and a `Literal` is exactly
# where a type checker has to see the members statically. `models.PERMIT_STATUSES`
# stays the single source of truth the CHECK is built from, and
# `test_models.py::test_the_schema_literals_match_the_tables_own_check_constraints`
# closes the gap (lesson: an enum-ish column has ONE source of truth — the tuple).
PermitStatus = Literal[
    "pending_signatures", "active", "suspended", "revoked", "expired", "archived"
]


def _trim_decimal(value: Decimal | None) -> str | None:
    """`area_ha`/`amount`/`sb_load` are fixed-scale NUMERIC, so a posted `2.6`
    round-trips as `Decimal('2.6000')`. `format(value, "f")` forces fixed-point
    first, so this never risks `Decimal.normalize()`'s scientific-notation
    surprise on a whole number (`Decimal('100.0000').normalize()` is
    `Decimal('1E+2')`). Same helper shape as `gis.schemas._trim_decimal`."""
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class PermitOut(BaseModel):
    """`POST /applications/{id}/permit` — the permit as it stands the moment it is
    formed: numbered, rendered, hash-frozen and awaiting four signatures."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    series: str
    number: int
    status: PermitStatus
    application_id: uuid.UUID
    applicant_id: uuid.UUID
    activity_type_id: uuid.UUID
    organization_id: uuid.UUID
    contour_id: uuid.UUID
    contour_version_id: uuid.UUID
    area_ha: Decimal
    period_from: date
    period_to: date
    amount: Decimal
    sb_load: Decimal | None
    pdf_file_id: uuid.UUID | None
    doc_hash: str | None
    template_id: uuid.UUID | None
    issued_at: datetime | None
    created_at: datetime

    @field_serializer("area_ha", "amount", "sb_load")
    def _serialize_decimal(self, value: Decimal | None) -> str | None:
        return _trim_decimal(value)
