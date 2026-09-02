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
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

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


class PermitSignIn(BaseModel):
    """`POST /permits/{id}/signatures`: one of the four ERI signature lines.

    `purpose` is a plain bounded string, deliberately NOT a `Literal` over
    `signers.PURPOSE_ROLES`. The required set is admin-editable data (ruling 7),
    so the schema must let an unmapped purpose through to the service, which
    refuses it with the accurate reason and RECORDS the attempt — a 422 from
    pydantic would be silent about a typo an operator just made in a settings
    row. The pattern still bounds it: a purpose is a short snake_case identifier,
    and this is the one field an unauthenticated-shaped body could stuff.

    There is no `document` field. The bytes signed are the permit's own stored
    PDF, never anything the client supplies (ruling 3) — a caller who could name
    the document could sign something other than the permit.
    """

    purpose: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
    pkcs7: Annotated[str, Field(min_length=1)]


class PermitSignatureOut(BaseModel):
    """What a signing screen needs after each of the four signatures: whether the
    permit is in force yet, and who has still to sign.

    `missing_signatures` is in the configured order, which is a DISPLAY order —
    signatures may be taken in any order (plan ruling 5), so a UI must not read
    the first entry as "whose turn it is".
    """

    status: PermitStatus
    missing_signatures: list[str]


# The four words `tz/04` С12 and `design/03` fix for the public page, spelled out
# by hand for the same reason `PermitStatus` above is: a `Literal`'s members must
# be statically visible to pyright, so `Literal[*PUBLIC_STATUS_LABELS.values()]`
# is not an option. `service.PUBLIC_STATUS_LABELS` stays the single source of
# truth and `test_public_check.py::test_every_permit_status_has_a_decided_public_
# answer` closes the gap (lesson: an enum-ish value has ONE source of truth).
PublicStatus = Literal["амалда", "тўхтатилган", "муддати тугаган", "бекор қилинган"]


class PublicCheckMiss(BaseModel):
    """What every lookup with nothing in force to show answers — an unknown
    token, an unknown series and number, and a permit nobody has signed yet, all
    the same shape.

    One key, and it is the whole body. A 404 for the unknown and a 200 for the
    known would let anyone walk the series and learn which numbers exist
    (ruling 8); so would a miss that carried one extra field the hit does not.
    """

    found: Literal[False] = False


class PublicCheckCard(BaseModel):
    """`GET /public/permits/check` — what a citizen or an inspector sees.

    Every field is either masked (`holder`), a reference name the permit already
    prints (`organization`, `activity_type`), or the document's own validity —
    and the list is closed: `qr_token` is the key to this very page and
    `holder_pinfl` is requisite 10's identity half, so neither may appear here at
    any width. `signatures_valid` is the STORED verification verdict of the 3+1
    signatures; nothing on this path calls E-IMZO.
    """

    found: Literal[True] = True
    status: PublicStatus
    valid_from: date
    valid_to: date
    organization: str
    activity_type: str
    signatures_valid: bool
    holder: str
