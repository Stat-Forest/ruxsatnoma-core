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
from typing import Annotated, Any, Literal

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
    """The permit's own columns — what `POST /applications/{id}/permit` answers
    the moment the document is formed (numbered, rendered, hash-frozen, awaiting
    four signatures), and what one row of `GET /permits` carries.

    One shape for both, deliberately: every field here has already been judged
    safe to return by the module docstring's two exclusions, and a second,
    narrower list item would be a second place to remember them."""

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


class PermitSignatureRow(BaseModel):
    """One of the four ERI signature lines, as the permit card shows it.

    A REDUCED view of a `signatures` row, not `signatures.schemas.SignatureOut`.
    Two fields are left out on purpose: `signature_value` is the whole PKCS#7
    envelope (kilobytes per line, four lines, on a screen that only needs to say
    who signed and whether it verified) and `verification` is the raw provider
    payload. 3.8's own `GET /signatures?object_type=permit&object_id=…` answers
    the full row for anyone who needs it, so this card does not have to — and
    defining the shape here rather than importing that module's schema keeps the
    two free to change independently.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    purpose: str
    signer_user_id: uuid.UUID | None
    certificate_id: uuid.UUID
    signed_at: datetime
    verification_status: str


class PermitHistoryRow(BaseModel):
    """One entry of the permit's timeline (`permit_status_history`).

    `reason_item_id`, `legal_basis` and `doc_file_id` are null for everything
    3.11a writes and are on the shape from day one: they are the legal ground of
    a suspension or a revocation (С13), and 3.11b fills them in on rows this very
    response already renders — a front end reading this card must not have to
    change its parser to see why a permit stopped working.
    """

    model_config = ConfigDict(from_attributes=True)

    from_status: PermitStatus | None
    to_status: PermitStatus
    reason_item_id: uuid.UUID | None
    legal_basis: str | None
    doc_file_id: uuid.UUID | None
    changed_by: uuid.UUID | None
    occurred_at: datetime


class PermitCardOut(PermitOut):
    """`GET /permits/{id}` — the permit's own columns, FLAT, plus the three lists
    that are not columns of `permits` at all.

    Flat rather than `{"permit": {...}, "signatures": [...]}`: the card and one
    row of the list are the same object seen at two depths, and nesting would
    make a client read `body["permit"]["status"]` here and `body["status"]`
    there for the identical fact.
    """

    signatures: list[PermitSignatureRow]
    history: list[PermitHistoryRow]
    missing_signatures: list[str]

    @classmethod
    def build(cls, card: dict[str, Any]) -> PermitCardOut:
        """Assemble the response from `service.permit_card`'s dict.

        `PermitOut.model_fields` is read rather than the twenty names retyped: a
        column added to `PermitOut` has to appear on the card too, and a
        hand-copied list is exactly how the two would drift. `model_validate`
        does the rest — including the nested rows, which arrive as ORM objects
        and are validated `from_attributes`.
        """
        permit = card["permit"]
        return cls.model_validate(
            {
                **{name: getattr(permit, name) for name in PermitOut.model_fields},
                "signatures": card["signatures"],
                "history": card["history"],
                "missing_signatures": card["missing_signatures"],
            }
        )


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


class DecisionIn(BaseModel):
    """The body `/suspend`, `/resume` and `/revoke` share (plan
    `03.11b-permits-lifecycle`, `lifecycle_router.py` and Task 4's own route).

    `legal_basis` and `doc_file_id` are optional at the SCHEMA level because
    their true requirement is PER-ACT (ruling 6: a document is required for
    `suspend`/`revoke`, and `PS-07` alone forces a non-blank `legal_basis`) —
    only the service knows which act is running, and a schema-level
    `Field(...)` cannot vary by the URL a body was posted to.
    """

    reason_item_id: uuid.UUID
    legal_basis: str | None = None
    doc_file_id: uuid.UUID | None = None
    pkcs7: Annotated[str, Field(min_length=1)]


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
