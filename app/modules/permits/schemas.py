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
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    model_validator,
)

from app.core.schemas import LocalizedName

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
    # Ruling #183: the holder's line may be a SIMPLE signature — `kind`
    # says so and `certificate_id` is then NULL. Stage 10's integration found
    # the card answering 500 the moment a citizen pressed the button: the
    # signature route itself was green, and only the next screen broke.
    kind: str
    certificate_id: uuid.UUID | None
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


class PermitRatingIn(BaseModel):
    """`POST /permits/{id}/rating` — the citizen's verdict on a permit they
    actually received (ruling #140). One per permit, checked by the service,
    never left to `permit_ratings`'s own UNIQUE index."""

    score: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


class PermitRatingOut(BaseModel):
    """One rating, exactly as `permit_ratings` stores it. No `permit_id`, no
    applicant: ruling #141 keeps the author off every response built from this
    table, and this is the shape every such response embeds."""

    model_config = ConfigDict(from_attributes=True)

    score: int
    comment: str | None
    created_at: datetime


class PermitCardOut(PermitOut):
    """`GET /permits/{id}` — the permit's own columns, FLAT, plus the three lists
    that are not columns of `permits` at all.

    Flat rather than `{"permit": {...}, "signatures": [...]}`: the card and one
    row of the list are the same object seen at two depths, and nesting would
    make a client read `body["permit"]["status"]` here and `body["status"]`
    there for the identical fact.

    `document_date` (demo-sprint defect, `docs/status.md` "`Berilgan sana`
    renders in UTC"): `issued_at` above is `permits.issued_at`, the ACTIVATION
    timestamp `service._activate` stamps in UTC when the last of the 3+1
    signatures completes — a card was the only place `issued_at` sat beside
    `signatures[].signed_at`, and the only date-like field this schema offered
    for "Берилган сана" was that UTC activation instant, which a caller then
    has to convert. The document itself already carries the RIGHT value, frozen
    Tashkent-local at issuance and printed on the PDF: `snapshot["issued_at"]`
    (`service._snapshot`'s own comment: "the calendar date the DOCUMENT bears,
    in Tashkent"). `document_date` surfaces exactly that stored string as a
    `date`, so a caller displaying "Берилган сана" needs no timezone
    arithmetic of its own to get it wrong — it reads the same calendar day the
    paper permit shows, never `issued_at`'s UTC clock digits.
    """

    signatures: list[PermitSignatureRow]
    history: list[PermitHistoryRow]
    missing_signatures: list[str]
    document_date: date
    rating: PermitRatingOut | None = None

    @classmethod
    def build(cls, card: dict[str, Any]) -> PermitCardOut:
        """Assemble the response from `service.permit_card`'s dict.

        `PermitOut.model_fields` is read rather than the twenty names retyped: a
        column added to `PermitOut` has to appear on the card too, and a
        hand-copied list is exactly how the two would drift. `model_validate`
        does the rest — including the nested rows, which arrive as ORM objects
        and are validated `from_attributes`. `document_date` is parsed here
        from `permit.snapshot["issued_at"]` — an ISO date string, `_snapshot`'s
        own format — rather than added to `PermitOut.model_fields`'s generic
        copy: `snapshot` itself stays excluded from every response (this
        schema's own module docstring), so one field is lifted out of it by
        name, never the whole blob. `rating` (Task 4) is `card["rating"]`
        unchanged — an ORM row or `None`, either way validated `from_attributes`
        by `PermitRatingOut`'s own config — so the cabinet needs one request for
        the permit and its rating together, not two.
        """
        permit = card["permit"]
        return cls.model_validate(
            {
                **{name: getattr(permit, name) for name in PermitOut.model_fields},
                "signatures": card["signatures"],
                "history": card["history"],
                "missing_signatures": card["missing_signatures"],
                "document_date": date.fromisoformat(permit.snapshot["issued_at"]),
                "rating": card["rating"],
            }
        )


class PermitSignIn(BaseModel):
    """`POST /permits/{id}/signatures`: one of the four ERI signature lines —
    or, since ruling #183, the holder's simple signature with no envelope at
    all.

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

    `pkcs7` is OPTIONAL (ruling #183): a citizen acting for themselves signs
    with a button, and posts a body carrying no envelope at all. Absent, it is
    NOT automatically a simple signature — `permits.service.add_signature`
    decides who may take that path (the holder purpose, `on_behalf='self'`)
    and refuses everyone else with `ERR-SIGN-001` `simple_signature_not_
    allowed`. WITH `pkcs7` present, nothing about this route changes for
    anyone, whatever the purpose or the application's `on_behalf`.
    """

    purpose: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
    pkcs7: Annotated[str | None, Field(min_length=1)] = None


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
    legal_basis: Annotated[str | None, Field(max_length=2000)] = None
    doc_file_id: uuid.UUID | None = None
    pkcs7: Annotated[str, Field(min_length=1)]


class DuplicateIn(BaseModel):
    """`POST /permits/{id}/duplicates` — the нусха register (plan
    `03.11b-permits-lifecycle` ruling 9). `reason` is the whole body: a
    duplicate carries no document and no ERI signature of its own, because it
    changes nothing about the permit — it points a new register row at the
    SAME `pdf_file_id` (`service.issue_duplicate`'s own docstring).

    `StringConstraints(strip_whitespace=True, ...)`, not a plain
    `Field(min_length=1, ...)`: a reason of pure whitespace has a nonzero
    length and would otherwise pass as if it said something.
    """

    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class DuplicateOut(BaseModel):
    """One row of the register — what both `POST` and `GET
    /permits/{id}/duplicates` answer.

    `file_id` is always the ORIGINAL permit's `pdf_file_id`: a duplicate is a
    copy of that one document, never a re-render, so every row of one
    permit's register names the identical file (ruling 9).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    permit_id: uuid.UUID
    reason: str
    file_id: uuid.UUID
    issued_by: uuid.UUID
    issued_at: datetime


class ForestTicketIn(BaseModel):
    """`POST /permits/{id}/forest-tickets` — one ўрмон чиптаси against an
    ACTIVE permit (ВМҚ 506, plan `03.11b-permits-lifecycle` ruling 11).

    `restrictions` is stored exactly as given and validated only as an
    object with string keys — the real ВМҚ 506 field list is `tz/12` #34 and
    inventing one now is ruling 11(б). The documented shape a caller is
    expected to send:

        {"fire_ban_days": [...], "allowed_tools": [...], "notes": "..."}

    `valid_to >= valid_from` is checked HERE rather than left for the DB
    CHECK (`forest_tickets.period_ordered`) to catch as an `IntegrityError`
    a caller would have to decode — the same reasoning `gis.schemas`'
    `_validate_period` already gives its own two callers (lesson: an enum-ish
    or ordered pair guarded by a DB CHECK is validated in the schema too, so
    the CHECK is never the first thing a caller meets).
    """

    valid_from: date
    valid_to: date
    restrictions: dict[str, Any]

    @model_validator(mode="after")
    def _check_period(self) -> Self:
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to must not be before valid_from")
        return self


# `models.FOREST_TICKET_STATUSES`, spelled out by hand for the same reason
# `PermitStatus` above is: pyright rejects a starred variable inside `Literal`,
# so a `Literal`'s members must be statically visible (lesson: an enum-ish
# column has ONE source of truth — the tuple). `test_forest_tickets.py`'s own
# guard test closes the gap.
ForestTicketStatus = Literal["active", "expired", "revoked"]


class ForestTicketOut(BaseModel):
    """One row of the ВМҚ 506 register — what `POST` answers the moment a
    ticket is issued, and what one row of `GET /permits/{id}/forest-tickets`
    carries."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str
    permit_id: uuid.UUID
    valid_from: date
    valid_to: date
    restrictions: dict[str, Any]
    status: ForestTicketStatus
    file_id: uuid.UUID | None
    issued_by: uuid.UUID
    created_at: datetime


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
    # The same status in every language the interfaces offer — the one field on
    # this card that is computed rather than quoted from the printed document,
    # and therefore the one that may be localized (stage 7.3, finding F6).
    # `service.PUBLIC_STATUS_LABELS_I18N` is its single source of truth, and
    # `status` above is that map's Cyrillic column, so the two can never disagree.
    status_label: LocalizedName
    valid_from: date
    valid_to: date
    organization: str
    activity_type: str
    signatures_valid: bool
    holder: str
    # The permit's map contour, as a GeoJSON geometry — `None` unless BOTH
    # `public_permit_contour_enabled` is on (ruling R2: personal geodata,
    # stays off until the Agency confirms in writing — see
    # `app/core/settings_store.py`) AND the permit's contour has a published
    # version to draw. The flag check lives in `service.public_check`, not
    # here and not in the router: a schema field only shapes what CAN be
    # sent, never decides what IS.
    contour: dict[str, Any] | None = None


# --- Task 5: the Agency's aggregates, without the author (ruling #142) --------
#
# `avg_score` fields carry NO custom `field_serializer`, deliberately unlike
# `PermitOut`'s `_trim_decimal`: `repo.py` rounds to two decimal places IN SQL
# (`ROUND(AVG(score), 2)`), so the `Decimal` that comes back already has scale
# 2 and pydantic's own default serialization prints it as `"4.00"`, not `"4"`
# — trimming trailing zeros here would throw away the very rounding the SQL
# did.


class RatingsBreakdownRow(BaseModel):
    """One group of `GET /admin/ratings/summary`'s two breakdowns — exactly
    one of `organization_id`/`activity_type_id` is set, depending on which
    list this row sits in."""

    organization_id: uuid.UUID | None = None
    activity_type_id: uuid.UUID | None = None
    name: dict[str, Any]
    avg_score: Decimal
    count: int


class RatingsSummaryOut(BaseModel):
    """`GET /admin/ratings/summary` — the overall average and count over the
    caller's zone and the given period, plus the same pair broken down by
    organization and by activity type. `avg_score`/`count` are both null-safe:
    zero ratings in scope reads as `avg_score: null, count: 0`, never a 404 or
    a division-by-zero — a summary has no row to refuse."""

    avg_score: Decimal | None
    count: int
    by_organization: list[RatingsBreakdownRow]
    by_activity_type: list[RatingsBreakdownRow]


class RatingCommentRow(BaseModel):
    """`GET /admin/ratings` — one row of the anonymous comment feed.

    Ruling #141: date, service, leshoz, score, text. No applicant, no permit
    number — anything that identifies WHO rated is absent by construction, not
    filtered out at render time. `test_comments_never_name_the_author` asserts
    this on the SERIALIZED body rather than on this class, on purpose: a field
    added here later would pass a field-name check and still leak.
    """

    created_at: datetime
    score: int
    comment: str | None
    organization_name: dict[str, Any]
    activity_type_name: dict[str, Any]
