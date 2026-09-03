"""API shapes for applications (plan 03.9a task 3; design/03 § Заявки).

Two rules run through this file and are worth stating once rather than per
class:

  * **Every input model forbids unknown fields.** A draft is autosaved field by
    field (ruling 7), so a client PATCHes small bodies constantly — and the one
    field it must NOT be able to set, `requested_area_ha`, is frozen at
    submission from the contour version's own `area_ha` (ruling 22). With
    `extra="ignore"` a client that sent it would be told 200 and believe it had
    set the area; `extra="forbid"` turns that into a 422 naming the field.
  * **The card is the list row plus what is not a column of `applications`.**
    `ApplicationCardOut` extends `ApplicationOut` FLAT, the shape
    `permits.schemas.PermitCardOut` already uses, so a client reads
    `body["status"]` in both places rather than `body["application"]["status"]`
    in one of them.

`checks` and `calculation` are on the card from THIS task even though nothing
writes either before tasks 4 and 5. They are a contract, not a placeholder:
`payments` and `permits` already read `card["calculation"]["amount"]`, and a
key that appears halfway through a stage is a key a front end learns twice.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

# Spelled out rather than `Literal[*APPLICATION_STATUSES]`: pyright rejects a
# starred variable inside `Literal` (`reportInvalidTypeForm`), and a `Literal`
# is exactly where a type checker has to see the members statically.
# `models.APPLICATION_STATUSES` stays the single source of truth the CHECK is
# built from, and `test_models.py::test_the_schema_literals_match_the_tuples_
# the_checks_are_built_from` closes the gap (lesson: an enum-ish column has ONE
# source of truth — the tuple).
ApplicationStatus = Literal[
    "DRAFT",
    "SUBMITTED",
    "IN_REVIEW",
    "PENDING_INFO",
    "RETURNED",
    "APPROVED",
    "INVOICED",
    "PAID",
    "PERMIT_ISSUED",
    "REJECTED",
    "CANCELLED",
    "EXPIRED_UNPAID",
    "CLOSED",
    "ARCHIVED",
]
OnBehalf = Literal["self", "legal"]
Channel = Literal["portal", "mygov"]
ApplicationKind = Literal["new", "extension"]

# `application_items.head_count` is a plain integer column, so the only ceiling
# it has is the one written here. Bounded for the same reason every integer
# query parameter is (`core.schemas.PAGING_MAX`): an unbounded integer reaches
# asyncpg as `DataError: value out of int64 range` — a 500 for a body anybody
# can post. A million head on one contour is already absurd by three orders of
# magnitude; the real limit is the norm's, checked by `norms.checks`.
MAX_HEAD_COUNT = 1_000_000
# `applications.quantity` is `NUMERIC(12, 4)` — 8 integer digits. `max_digits`
# and `decimal_places` below are that column, restated where a 422 is still
# possible; without them an over-precise value reaches Postgres as a
# `NumericValueOutOfRange` 500.
QUANTITY_MAX_DIGITS = 12
QUANTITY_DECIMAL_PLACES = 4


def _trim_decimal(value: Decimal | None) -> str | None:
    """A fixed-scale NUMERIC as a client should read it: posting `"2.6"` into
    `NUMERIC(12,4)` round-trips as `Decimal('2.6000')` (lesson). `format(value,
    "f")` forces fixed-point first, so this never risks `Decimal.normalize()`'s
    scientific-notation surprise on a whole number (`Decimal('100.0000')
    .normalize()` is `Decimal('1E+2')`). Same helper shape as
    `gis.schemas._trim_decimal` and `permits.schemas._trim_decimal`."""
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class ApplicationCreate(BaseModel):
    """`POST /applications` — the whole body. Everything else about a draft
    arrives through PATCH (ruling 7).

    `applicant_id` is meaningful only with `on_behalf="legal"`: for `"self"` the
    applicant is the caller's own `applicants` row and naming somebody else's
    would be the first half of filing in another citizen's name. The service
    refuses the mismatch rather than a validator here, so the refusal carries a
    domain reason instead of a pydantic field error.
    """

    model_config = ConfigDict(extra="forbid")

    on_behalf: OnBehalf
    applicant_id: uuid.UUID | None = None


class ApplicationItemIn(BaseModel):
    """One livestock line of a grazing application."""

    model_config = ConfigDict(extra="forbid")

    livestock_type_id: uuid.UUID
    head_count: Annotated[int, Field(gt=0, le=MAX_HEAD_COUNT)]


class ApplicationPatch(BaseModel):
    """`PATCH /applications/{id}` — any SUBSET of the draft's own fields
    (design/03's list, plan task 3). A field left out is untouched; a field sent
    as `null` is cleared, which is what makes a half-filled draft correctable.

    `items` is `None` (absent) or the COMPLETE new list — replaced wholesale,
    never merged, because an applicant removing a livestock kind must be able
    to and merge semantics would make that impossible.

    No period ordering check and no completeness check: ruling 7 puts both in
    the pre-check and the submission. What IS checked here is every FK
    (`service._assert_references`), because an unknown id would otherwise reach
    `flush()` as an `IntegrityError` — an ERR-SYS-001/500 for a plain typo
    (lesson: walk every caller-settable FK before `flush()`).

    `quantity` is the declared amount for every activity that is not grazing —
    hectares for haymaking, m³ for deadwood, hives for an apiary
    (`norms.calculator.CalcRequest`'s own definition). Editable here, required
    at submission for those activities (task 5).
    """

    model_config = ConfigDict(extra="forbid")

    activity_type_id: uuid.UUID | None = None
    contour_id: uuid.UUID | None = None
    period_from: date | None = None
    period_to: date | None = None
    # `allow_inf_nan=False` before any bound is applied: `Decimal("NaN")`
    # parses, and the ordering comparison a `ge` performs raises
    # `InvalidOperation` — an `ArithmeticError`, which pydantic does not turn
    # into a 422, so it escapes as a 500 (lesson, hit twice on `POST /tariffs`).
    quantity: (
        Annotated[
            Decimal,
            Field(
                ge=0,
                allow_inf_nan=False,
                max_digits=QUANTITY_MAX_DIGITS,
                decimal_places=QUANTITY_DECIMAL_PLACES,
            ),
        ]
        | None
    ) = None
    items: list[ApplicationItemIn] | None = None
    benefit_category_item_id: uuid.UUID | None = None


class ApplicationItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    livestock_type_id: uuid.UUID
    head_count: int


class ApplicationDocumentOut(BaseModel):
    """One attachment, as `POST /applications/{id}/documents` answers and as the
    card lists it. `uploaded_by` is deliberately absent: on a draft it is always
    the applicant themselves, and 3.9b's staff-side uploads get their own
    read."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doc_type_item_id: uuid.UUID
    file_id: uuid.UUID
    note: str | None
    created_at: datetime


class ApplicationCheckOut(BaseModel):
    """One check result — evidence, and evidence is a LIST: every run is kept
    and none is superseded (ruling 12), so a card shows the history rather than
    "the latest per type"."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    check_type: str
    result: str
    details: Any
    source: str
    checked_at: datetime


class ApplicationCalculationOut(BaseModel):
    """The application's CURRENT price — `applications.service.
    current_calculation`, which is the newest `calculations` row and exactly
    what `payments` invoices from.

    A reduced view of that row on purpose: `input_snapshot` and `breakdown` are
    the calculator's own JSON, sometimes kilobytes, and `GET /calculations/{id}`
    (norms' own route) answers the full row for whoever needs it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    amount: Decimal
    rule_version: str
    used_sb: Decimal | None
    max_sb: int | None
    remaining_sb: Decimal | None
    created_at: datetime

    @field_serializer("amount", "used_sb", "remaining_sb")
    def _serialize_decimal(self, value: Decimal | None) -> str | None:
        return _trim_decimal(value)

    @classmethod
    def build(cls, calculation: Any) -> ApplicationCalculationOut:
        """`rule_version` is `calculations.rule_code_version` under the name the
        CARD uses for it — plan task 8's own assertion is
        `card["calculation"]["rule_version"] is not None`, and this task fixes
        the shape that assertion reads.

        Deliberately NOT the column's own name, which `norms.schemas.
        CalculationOut` keeps for `GET /calculations/{id}`: there the field sits
        beside `rule_parameters` and the distinction between the arithmetic's
        version and a parameter's matters, while on an application card there is
        one version and nothing to tell it apart from. The mapping lives here,
        in one place, so neither response has to know about the other."""
        return cls.model_validate(
            {
                "id": calculation.id,
                "amount": calculation.amount,
                "rule_version": calculation.rule_code_version,
                "used_sb": calculation.used_sb,
                "max_sb": calculation.max_sb,
                "remaining_sb": calculation.remaining_sb,
                "created_at": calculation.created_at,
            }
        )


class ApplicationOut(BaseModel):
    """The application's own columns — the response to create and patch, and one
    row of `GET /applications`.

    One shape for all three, deliberately: a second, narrower list item would be
    a second place to remember what an application may say about itself.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str | None
    status: ApplicationStatus
    applicant_id: uuid.UUID
    submitted_by_user_id: uuid.UUID
    on_behalf: OnBehalf
    representation_id: uuid.UUID | None
    activity_type_id: uuid.UUID | None
    contour_id: uuid.UUID | None
    contour_version_id: uuid.UUID | None
    requested_area_ha: Decimal | None
    period_from: date | None
    period_to: date | None
    quantity: Decimal | None
    channel: Channel
    kind: ApplicationKind
    benefit_category_item_id: uuid.UUID | None
    rejection_reason_item_id: uuid.UUID | None
    assigned_org_id: uuid.UUID | None
    assigned_user_id: uuid.UUID | None
    parent_application_id: uuid.UUID | None
    sla_deadline_at: datetime | None
    submitted_at: datetime | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("requested_area_ha", "quantity")
    def _serialize_decimal(self, value: Decimal | None) -> str | None:
        return _trim_decimal(value)


class ApplicationCardOut(ApplicationOut):
    """`GET /applications/{id}` — the columns above, flat, plus the four things
    that are not columns of `applications` at all."""

    items: list[ApplicationItemOut]
    documents: list[ApplicationDocumentOut]
    checks: list[ApplicationCheckOut]
    calculation: ApplicationCalculationOut | None

    @classmethod
    def build(cls, card: dict[str, Any]) -> ApplicationCardOut:
        """Assemble the response from `service.get_card`'s dict.

        `ApplicationOut.model_fields` is read rather than the twenty-four names
        retyped: a column added to `ApplicationOut` has to appear on the card
        too, and a hand-copied list is exactly how the two would drift
        (`permits.schemas.PermitCardOut.build` is the same shape, for the same
        reason)."""
        application = card["application"]
        calculation = card["calculation"]
        return cls.model_validate(
            {
                **{name: getattr(application, name) for name in ApplicationOut.model_fields},
                "items": card["items"],
                "documents": card["documents"],
                "checks": card["checks"],
                "calculation": (
                    None if calculation is None else ApplicationCalculationOut.build(calculation)
                ),
            }
        )


# --- Task 4: documents and the pre-check -------------------------------------


class ApplicationDocumentIn(BaseModel):
    """`POST /applications/{id}/documents` — one attachment.

    `file_id` names a `media_files` row the caller has ALREADY uploaded through
    `POST /files`; the service checks it exists, is active and is the caller's
    own (`service._own_document_file`), because a file id an applicant supplies
    is untrusted input.
    """

    model_config = ConfigDict(extra="forbid")

    doc_type_item_id: uuid.UUID
    file_id: uuid.UUID
    note: str | None = None


class PrecheckCalculationOut(BaseModel):
    """The price a pre-check quotes — `norms.service.preview`'s answer, which is
    written NOWHERE (ruling 8: exactly one calculation is stored, at
    submission).

    Not `ApplicationCalculationOut`: that one describes a stored `calculations`
    row and carries its `id` and `created_at`, neither of which a dry run has.
    The overlapping fields keep the card's names (`rule_version`, not the
    column's `rule_code_version`) so a client reads one vocabulary.

    Every `Decimal` arrives here already rendered as a string by
    `norms.calculator.jsonable` — money and conditional heads round-trip
    exactly as text and would not as floats.
    """

    model_config = ConfigDict(extra="forbid")

    amount: str
    used_sb: str | None = None
    max_sb: int | None = None
    remaining_sb: str | None = None
    rule_version: str
    breakdown: list[Any] = []

    @classmethod
    def build(cls, priced: dict[str, Any]) -> PrecheckCalculationOut:
        return cls.model_validate(
            {
                "amount": priced["amount"],
                "used_sb": priced["used_sb"],
                "max_sb": priced["max_sb"],
                "remaining_sb": priced["remaining_sb"],
                "rule_version": priced["rule_code_version"],
                "breakdown": priced["breakdown"],
            }
        )


class PrecheckOut(BaseModel):
    """`POST /applications/{id}/precheck` — what the checks said, and what it
    would cost.

    A blocking GIS or norm result is IN `checks`, as data, and the response is
    still 200 (design/03, and 3.7's own `calc_router` docstring): the applicant
    has to be able to see that the herd is over the limit, not merely be
    refused. Task 5's submission runs the very same `checks.run_all` and turns
    that same result into an HTTP error.

    `calculation` is null when the draft is not complete enough to price — the
    fields still missing are named in each `skipped` check's own `details`.
    """

    checks: list[ApplicationCheckOut]
    calculation: PrecheckCalculationOut | None


# --- Task 5: the submission ---------------------------------------------------


class ApplicationSubmitIn(BaseModel):
    """`POST /applications/{id}/submit` — the detached PKCS#7 the client
    produced over the bytes `GET /applications/{id}/package` served, and
    nothing else.

    The package itself is deliberately NOT echoed back in the body: the server
    signs what IT computes (`service._package_bytes`), and a client-supplied
    copy would only give an attacker a second thing to disagree with. What the
    client signed is proven by the signature verifying, not by it being
    re-sent.
    """

    model_config = ConfigDict(extra="forbid")

    pkcs7: str
