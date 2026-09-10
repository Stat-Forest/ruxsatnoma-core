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
# `HISTORY_STATUSES` (plan 12, R7): what a timeline row may say — `DRAFT`
# included, as the past value every pre-stage-12 filing's first row carries.
HistoryStatus = Literal[
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
# `application_conclusions.kind`/`.recommendation` (task 5, 3.9b) — the same
# CHECK-backed-tuple shape as the four above, guarded by the same test.
ConclusionKind = Literal["executor", "gis"]
ConclusionRecommendation = Literal["approve", "reject"]
# Ruling #179: `models.BENEFIT_VERIFICATION_STATUSES`, spelled out for the
# identical pyright reason the five Literals above are — a starred variable
# is rejected inside `Literal`, and `test_the_schema_literals_match_the_
# tuples_the_checks_are_built_from` is the guard that keeps the two in sync.
BenefitVerificationStatus = Literal["not_required", "pending", "verified", "rejected"]

# `POST /applications/{id}/checks` (task 7, 3.9b) — deliberate SUBSETS of
# `models.CHECK_TYPES`/`CHECK_RESULTS`/`CHECK_SOURCES`, not their mirror, so
# NOT added to `test_the_schema_literals_match_the_tuples_the_checks_are_
# built_from`: this route only ever writes `check_type in ("vet", "cadastre")`
# (the auto GIS/norm ones go through `checks.run_all` alone) and never
# `result="skipped"` (that is `gis`/`norms`' own designed branch for an empty
# reference layer — an unreachable vet/cadastre registry is the
# manual-fallback path instead, ApplicationCheckIn's own docstring) or
# `source="auto"` (reserved for `checks.run_all`).
ExternalCheckType = Literal["vet", "cadastre"]
CheckResult = Literal["pass", "fail", "warning"]

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
# `applications.benefit_certificate_no` is unbounded TEXT (ruling #179 — no
# `tz/` document prescribes a format, the Agency's certificate registries vary
# by category), so this is the only ceiling it has, the same reasoning
# `MAX_HEAD_COUNT` above states for an integer column.
BENEFIT_CERTIFICATE_NO_MAX_LENGTH = 200


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
    # Ruling #179: the certificate a certificate-requiring benefit category
    # needs. Free to leave blank while the claim is still being typed —
    # `applications.service.submit` is where "requires the certificate number
    # at submission" is enforced (this module's report to the integrator),
    # the same split `requested_area_ha` draws between PATCH (unconstrained)
    # and submission (where completeness actually matters).
    benefit_certificate_no: (
        Annotated[str, Field(max_length=BENEFIT_CERTIFICATE_NO_MAX_LENGTH)] | None
    ) = None


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
    "the latest per type".

    `created_by`/`confirmed_by`/`confirmed_at` are task 7's maker-checker
    columns (migration `0025`): every row names who created it, and only a
    manual paper result that has actually been confirmed carries the other
    two — `confirmed_by is None` is exactly "not usable yet" on the wire.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    check_type: str
    result: str
    details: Any
    source: str
    checked_at: datetime
    created_by: uuid.UUID
    confirmed_by: uuid.UUID | None
    confirmed_at: datetime | None


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


class ApplicationConclusionOut(BaseModel):
    """One specialist's written finding (task 5, 3.9b; tz/04 С8) — as `POST
    /applications/{id}/conclusion` answers the one it just wrote, and as the
    card lists them.

    Immutable (ruling 10): a correction is a NEW row, so — like
    `ApplicationCheckOut` beside it — the card's `conclusions` is the FULL
    list, never "the latest per kind"."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_id: uuid.UUID
    kind: ConclusionKind
    text: str
    recommendation: ConclusionRecommendation | None
    created_at: datetime


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
    # Ruling #179 — read-only on this shape (never accepted by
    # `ApplicationPatch`, which forbids unknown fields): `benefit_certificate_
    # no` is the one exception, editable through the patch above.
    benefit_certificate_no: str | None
    benefit_verification_status: BenefitVerificationStatus
    benefit_verified_by: uuid.UUID | None
    benefit_verified_at: datetime | None
    benefit_rejection_reason: str | None
    rejection_reason_item_id: uuid.UUID | None
    assigned_org_id: uuid.UUID | None
    assigned_user_id: uuid.UUID | None
    parent_application_id: uuid.UUID | None
    sla_deadline_at: datetime | None
    submitted_at: datetime | None
    decided_at: datetime | None
    # Ruling #184: when "I have read the rules" was accepted, server-stamped —
    # `None` until the first real submission.
    rules_accepted_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("requested_area_ha", "quantity")
    def _serialize_decimal(self, value: Decimal | None) -> str | None:
        return _trim_decimal(value)


class ApplicationCardOut(ApplicationOut):
    """`GET /applications/{id}` — the columns above, flat, plus the six things
    that are not columns of `applications` at all."""

    items: list[ApplicationItemOut]
    documents: list[ApplicationDocumentOut]
    checks: list[ApplicationCheckOut]
    calculation: ApplicationCalculationOut | None
    # Task 4 (3.9b), ruling 8: whether the SLA clock is running late RIGHT NOW —
    # `sla.is_overdue`, computed by `service.get_card` because it needs both
    # `status` (an OPEN pause suspends the clock whatever the stored deadline
    # says) and the wall clock, neither of which a schema should read for
    # itself. Not a column, so it belongs beside `calculation` here rather than
    # on `ApplicationOut`, which also serves the list row and create/patch —
    # design/03's own `sla_overdue=true` filter is a LIST feature nothing in
    # this task adds.
    sla_overdue: bool
    # Task 5 (3.9b), tz/04 С8: every conclusion on record — "the rahbar sees
    # both conclusions" — never just the newest per `kind` (ruling 10).
    conclusions: list[ApplicationConclusionOut]

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
                "conclusions": card["conclusions"],
                "calculation": (
                    None if calculation is None else ApplicationCalculationOut.build(calculation)
                ),
                "sla_overdue": card["sla_overdue"],
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


class PrecheckCheckOut(BaseModel):
    """One check result as a pre-check reports it — no row id, no author, no
    timestamp: since stage 12 a pre-check over a filing writes nothing (plan
    12, R3), and the per-id one on a RETURNED application answers in the same
    shape (R5) so a client has one thing to render."""

    check_type: str
    result: str
    details: Any


class PrecheckOut(BaseModel):
    """`POST /applications/precheck` (a filing, stage 12) and `POST
    /applications/{id}/precheck` (a RETURNED application) — what the checks
    said, and what it would cost.

    A blocking GIS or norm result is IN `checks`, as data, and the response is
    still 200 (design/03, and 3.7's own `calc_router` docstring): the applicant
    has to be able to see that the herd is over the limit, not merely be
    refused. The filing/submission runs the very same checks and turns that
    same result into an HTTP error.

    `calculation` is null when the filing is not complete enough to price —
    the fields still missing are named in each `skipped` check's own
    `details`.
    """

    checks: list[PrecheckCheckOut]
    calculation: PrecheckCalculationOut | None


# --- Task 5: the submission ---------------------------------------------------


class ApplicationSubmitIn(BaseModel):
    """`POST /applications/{id}/submit` — the detached PKCS#7 the client
    produced over the bytes `GET /applications/{id}/package` served, plus
    ruling #184's mandatory acceptance.

    The package itself is deliberately NOT echoed back in the body: the server
    signs what IT computes (`service._package_bytes`), and a client-supplied
    copy would only give an attacker a second thing to disagree with. What the
    client signed is proven by the signature verifying, not by it being
    re-sent.

    **`pkcs7` is now OPTIONAL** (ruling #183): a citizen filing for themselves
    (`on_behalf="self"`) signs with the button and posts no envelope at all —
    `service.submit` calls `signatures.service.sign_simple` over the SAME
    package bytes `sign()` would otherwise verify. A legal entity, or an
    envelope actually posted, is unchanged: `sign()` runs exactly as before.

    **`rules_accepted` is mandatory** (ruling #184, decisions.md): `false`
    (the default, so an old client that never learned the field is refused
    rather than silently accepted) is one of the fields `service._assert_
    complete` treats as MISSING — 400 `ERR-APP-001` naming `rules_accepted`
    alongside `contour_id`/`activity_type_id`/etc., not a separate check with
    its own reason. The server stamps `applications.rules_accepted_at` from
    its OWN clock; the client's claim is a gate, never a timestamp source.
    """

    model_config = ConfigDict(extra="forbid")

    pkcs7: str | None = None
    rules_accepted: bool = False


# --- Stage 12: the filing ------------------------------------------------------
#
# `DRAFT` is gone (plan 12): an application is created by ONE request carrying
# everything (`POST /applications`, `ApplicationFileIn`), and the two reads a
# client needs before it — the pre-check and the package to sign — take the
# same content without an id (`ApplicationFilingIn`). Every field a draft used
# to collect through PATCH is here, with the same bounds `ApplicationPatch`
# applies. Optional on purpose: the pre-check answers an incomplete filing
# with the fields still to fill (`checks.missing_for_pricing`), exactly as it
# answered a half-empty draft, and the filing itself refuses one with 400
# `ERR-APP-001`.


class ApplicationFilingIn(BaseModel):
    """The content of a filing — who, what, where, when, how much, the benefit
    claim and the documents. The body of `POST /applications/precheck` and
    `POST /applications/package`, and the base of `ApplicationFileIn`.

    `applicant_id` is meaningful only with `on_behalf="legal"`: for `"self"`
    the applicant is the caller's own `applicants` row and naming somebody
    else's is refused by the service with a domain reason
    (`applicant_is_not_the_caller`), never silently ignored.

    `documents` carry file ids already uploaded through `POST /files` (plan
    12, R9); each must be the caller's own active upload."""

    model_config = ConfigDict(extra="forbid")

    on_behalf: OnBehalf
    applicant_id: uuid.UUID | None = None
    activity_type_id: uuid.UUID | None = None
    contour_id: uuid.UUID | None = None
    period_from: date | None = None
    period_to: date | None = None
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
    items: list[ApplicationItemIn] = []
    benefit_category_item_id: uuid.UUID | None = None
    benefit_certificate_no: (
        Annotated[str, Field(max_length=BENEFIT_CERTIFICATE_NO_MAX_LENGTH)] | None
    ) = None
    documents: list[ApplicationDocumentIn] = []


class ApplicationFileIn(ApplicationFilingIn):
    """`POST /applications` — the filing (`ApplicationFilingIn`) plus what only
    the act of filing carries: ruling #184's acceptance, ruling #183's optional
    envelope, and — with the envelope — the `application_id` the package named
    (plan 12, R2). `pkcs7` and `application_id` travel together or not at
    all: the service refuses one without the other."""

    rules_accepted: bool = False
    pkcs7: str | None = None
    application_id: uuid.UUID | None = None


class ApplicationCloneOut(ApplicationFilingIn):
    """`GET /applications/{id}/clone` — an `ApplicationFilingIn` the caller may
    post back as it is. A subclass under its own name ON PURPOSE: a pydantic
    model used both as a request body and as a response splits into
    `-Input`/`-Output` variants in the OpenAPI document, and the generated
    client (`openapi-typescript`) then has no `ApplicationFilingIn` at all."""


class FilingPackageOut(BaseModel):
    """`POST /applications/package` — the id the application WILL have (plan
    12, R2: the signed bytes name it, so it is minted here and sent back with
    the signature) and the canonical bytes, base64 so the answer is JSON."""

    application_id: uuid.UUID
    package: str


# --- Task 6: cancelling, and the timeline -------------------------------------

# `application_status_history.reason_text` is unbounded TEXT, so the only
# ceiling a withdrawal reason has is the one written here — the same reasoning
# `MAX_HEAD_COUNT` above spells out for an integer column.
REASON_MAX_LENGTH = 2000


class ApplicationCancelIn(BaseModel):
    """`POST /applications/{id}/cancel` — an OPTIONAL free-text reason, stored
    as the history row's `reason_text`.

    Optional because `tz/05` asks for no ground to withdraw one's own
    application: a citizen who changes their mind owes nobody an explanation.
    That is the opposite of task 7's rejection, which is a refusal BY the state
    and needs an RJ-* reason and a legal basis before the signature is even
    checked.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, Field(max_length=REASON_MAX_LENGTH)] | None = None


class ApplicationAssignIn(BaseModel):
    """`POST /applications/{id}/assign` — `sys_admin` only (Task 1 ANSWERED
    (б), 2026-09-05). `reason` is restricted to the two HUMAN values
    `application_assignments.reason`'s CHECK allows for a manual act —
    `"auto"` is `assignment.py`'s own, never a client's to name.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    reason: Literal["manual", "absence"]


class TimelineSignatureRow(BaseModel):
    """One signature as the timeline shows it — ERI, or a citizen's simple
    one (ruling #183: `kind`, and then `certificate_id` is NULL).

    A REDUCED view of a `signatures` row, not `signatures.schemas.SignatureOut`
    — exactly the choice `permits.schemas.PermitSignatureRow` made and for the
    same two reasons: `signature_value` is the whole PKCS#7 envelope (kilobytes,
    on a screen that only needs to say who signed and whether it verified) and
    `verification` is the raw provider payload. 3.8's own `GET /signatures?
    object_type=…&object_id=…` answers the full row for whoever needs it, and
    defining the shape here keeps the two modules free to change independently.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    object_type: str
    object_id: uuid.UUID
    purpose: str
    signer_user_id: uuid.UUID | None
    # Stage 10's review found the timeline answering 500 for every filing made
    # with the button — the same defect the permit card had, one screen over:
    # the route that took the signature was green and the next read broke.
    kind: str
    certificate_id: uuid.UUID | None
    signed_at: datetime
    verification_status: str


class TimelineHistoryRow(BaseModel):
    """One transition, with the signature bound to THAT transition.

    `signatures` here is the SUBMISSION line (ruling 25): `submit` signs
    `("application_submission", <this row's id>)`, so the envelope resolves to
    the exact attempt it covers. Every other row carries `[]` — a DRAFT or an
    IN_REVIEW transition is nobody's signed act. The DECISION signature is not
    here at all: it belongs to the application, not to a row of its history, and
    sits at the top level of `ApplicationTimelineOut`.

    `reason_item_id`, `legal_basis` and `fields_to_fix` are null for everything
    3.9a writes and are on the shape from day one: they are 3.9b's return
    reasons and task 7's rejection grounds, filled in on rows this very read
    already returns.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_status: HistoryStatus | None
    to_status: HistoryStatus
    changed_by: uuid.UUID | None
    reason_item_id: uuid.UUID | None
    reason_text: str | None
    legal_basis: str | None
    fields_to_fix: dict[str, Any] | None
    occurred_at: datetime
    signatures: list[TimelineSignatureRow] = []

    @classmethod
    def build(cls, entry: Any, signatures: list[Any]) -> TimelineHistoryRow:
        """`model_fields` is read rather than the nine names retyped, the same
        way `ApplicationCardOut.build` assembles the card: a column added here
        must be read off the row, and a hand-copied list is how the two
        drift."""
        return cls.model_validate(
            {
                **{name: getattr(entry, name) for name in cls.model_fields if name != "signatures"},
                "signatures": [TimelineSignatureRow.model_validate(row) for row in signatures],
            }
        )


class TimelineInfoRequestRow(BaseModel):
    """One row of the `info_requests` register — final whole-branch review,
    IMPORTANT: the pause it records is the one event on this branch that
    silently moves a legally-consequential deadline (`sla_deadline_at`), and
    this is the only audit view that shows it happened at all. `responded_at`/
    `response_text` are `None` for a still-open pause, the same shape the
    table itself carries."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    requested_by: uuid.UUID
    message: str
    requested_at: datetime
    responded_at: datetime | None
    response_text: str | None


class TimelineAssignmentRow(BaseModel):
    """One row of the assignment register — who held the application, from when,
    and whether they still do.

    The SUPERSEDED rows are returned too, not only the active one: the register
    is the record of who held it when, and task 7's over-limit forward is
    readable only as two rows, the reviewer's deactivated and the parent
    organization's active.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    user_id: uuid.UUID | None
    assigned_by: uuid.UUID | None
    reason: str
    is_active: bool
    created_at: datetime


class ApplicationTimelineOut(BaseModel):
    """`GET /applications/{id}/timeline` — design/03's four keys.

    `signatures` at this level is the DECISION signature (`("application",
    <id>, "application_decision")`), which is why it is empty for everything
    3.9a can produce before task 7 lands and stays empty on an over-limit
    forward, where nothing is signed. The submission signatures are NOT here:
    each sits on its own `status_history` entry, which is the whole point of
    ruling 25 giving the history row and the signed object the same id.

    `info_requests` lists every pause this application has had, open or
    closed, oldest first (final whole-branch review, IMPORTANT).
    """

    status_history: list[TimelineHistoryRow]
    assignments: list[TimelineAssignmentRow]
    signatures: list[TimelineSignatureRow]
    info_requests: list[TimelineInfoRequestRow]

    @classmethod
    def build(cls, timeline: dict[str, Any]) -> ApplicationTimelineOut:
        return cls(
            status_history=[
                TimelineHistoryRow.build(row["entry"], row["signatures"])
                for row in timeline["status_history"]
            ],
            assignments=[
                TimelineAssignmentRow.model_validate(row) for row in timeline["assignments"]
            ],
            signatures=[TimelineSignatureRow.model_validate(row) for row in timeline["signatures"]],
            info_requests=[
                TimelineInfoRequestRow.model_validate(row) for row in timeline["info_requests"]
            ],
        )


# --- Task 7: the head's decision ----------------------------------------------

# `applications.decision_basis` and `application_status_history.legal_basis` are
# unbounded TEXT, so the only ceiling a legal basis has is the one written here
# — the same reasoning `REASON_MAX_LENGTH` above spells out for a withdrawal
# reason.
LEGAL_BASIS_MAX_LENGTH = 2000


class ApplicationApproveIn(BaseModel):
    """`POST /applications/{id}/approve` — the head's detached PKCS#7 over the
    bytes `GET /applications/{id}/package` served, and nothing else.

    Identical in shape to `ApplicationSubmitIn` and deliberately its own class:
    the two sign the same bytes for different reasons and by different people,
    and a shared model would make a later divergence look like a rename.
    """

    model_config = ConfigDict(extra="forbid")

    pkcs7: str


class ApplicationRejectIn(BaseModel):
    """`POST /applications/{id}/reject` — the ERI plus the grounds `tz/04` С8
    requires of a refusal BY the state: an RJ-* reason from the
    `rejection_reasons` classifier AND a legal basis.

    **`reason_item_id` is REQUIRED here rather than validated in the
    service**, which is what makes a missing one a 422 `ERR-VAL-001` before
    the request body is ever handed to a function that could reach `sign()` —
    a signature must never be spent on a request that cannot succeed.

    **`legal_basis` is OPTIONAL** (ruling #182): when the application's own
    benefit claim was `rejected` by the leshoz's own verify/reject pair, that
    verdict IS the grounds for rejecting the application too, and the head
    need not retype it — `decision.reject` fills `legal_basis` from the
    claim's own `benefit_rejection_reason` when the caller leaves it out.
    Every OTHER case keeps the ORIGINAL rule intact: a missing `legal_basis`
    is refused (`ERR-VAL-001`, `reason="legal_basis_required"`) before
    `sign()` is ever reached, exactly as when it was required at the wire.
    `min_length=1` closes the half a plain `str` would leave open when one
    IS given: an empty legal basis is a missing one.

    This is the opposite of `ApplicationCancelIn` beside it, whose reason is
    optional because a citizen withdrawing their own application owes nobody an
    explanation.
    """

    model_config = ConfigDict(extra="forbid")

    pkcs7: str
    reason_item_id: uuid.UUID
    legal_basis: Annotated[str, Field(min_length=1, max_length=LEGAL_BASIS_MAX_LENGTH)] | None = (
        None
    )


class ApplicationReturnIn(BaseModel):
    """`POST /applications/{id}/return` — task 3 (3.9b): send an application
    back for correction, with a typed reason, the fields to fix, and a legal
    basis.

    **No `pkcs7` here, unlike `ApplicationApproveIn`/`ApplicationRejectIn`** —
    returning a package for correction is not a decision the state signs
    (`applications.review`, the hodim's own permission, holds no ERI purpose
    at all); only approve/reject spend one.

    `legal_basis` is required with the same `min_length=1` as
    `ApplicationRejectIn`'s own, closing the identical gap a plain `str` would
    leave open. `fields_to_fix` is a JSON **OBJECT** — field name -> what is
    wrong with it, e.g. `{"period_to": "срок выходит за пределы сезона
    выпаса"}` — never a bare list of names, which would tell the applicant
    WHAT to fix but not why; `ApplicationStatusHistory.fields_to_fix` and
    `TimelineHistoryRow.fields_to_fix` are both `dict[str, Any] | None` for
    exactly this shape. Pydantic checks the TYPE only — that it is non-empty
    and that its keys name real columns of the application is the service's
    own check (`service.return_to_applicant`), which needs the row to answer
    "real column of THIS application".
    """

    model_config = ConfigDict(extra="forbid")

    reason_item_id: uuid.UUID
    fields_to_fix: dict[str, Any]
    legal_basis: Annotated[str, Field(min_length=1, max_length=LEGAL_BASIS_MAX_LENGTH)]


class ApplicationRequestInfoIn(BaseModel):
    """`POST /applications/{id}/request-info` — task 4 (3.9b): the reviewer
    asks the applicant for more information, opening the `info_requests` row
    that pauses the SLA clock (`sla.py`, ruling 8) until `respond-info` closes
    it.

    `message` is required and non-empty (`min_length=1`, the same gap
    `ApplicationRejectIn`'s own `legal_basis` closes) — a paused clock with
    nothing asked for leaves the applicant with no way to answer.
    """

    model_config = ConfigDict(extra="forbid")

    message: Annotated[str, Field(min_length=1)]


class ApplicationRespondInfoIn(BaseModel):
    """`POST /applications/{id}/respond-info` — the applicant's own reply,
    closing the newest open `info_requests` row and resuming the SLA clock by
    the length of the pause (ruling 8).

    `file_ids` names already-uploaded `media_files` rows — the bytes go
    through `POST /files` first, the same two-step `ApplicationDocumentIn`
    uses — and every one must be the caller's OWN active upload
    (`service._own_document_file`). An empty list is a text-only reply and is
    legal: not every request for information needs a document back.
    """

    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=1)]
    file_ids: list[uuid.UUID]


class ApplicationConclusionIn(BaseModel):
    """`POST /applications/{id}/conclusion` — task 5 (3.9b): a specialist's
    written finding (tz/04 С8), immutable (ruling 10 — no PATCH, no DELETE; a
    correction is a new row, never an edit of this one).

    `kind` names WHICH specialist is writing and is not decoration:
    `service.add_conclusion` gates each value on its own permission —
    `"executor"` on `applications.review` (the hodim), and `"gis"` refused
    with `ERR-ACL-001` for EVERY caller today, because `app/modules/gis/
    permissions.py` registers no code yet that means "authorised to write an
    application conclusion" (see that function's docstring — a gap for
    `decisions.md`/`design/03`, not something this schema can paper over).
    """

    model_config = ConfigDict(extra="forbid")

    kind: ConclusionKind
    text: Annotated[str, Field(min_length=1)]
    recommendation: ConclusionRecommendation | None = None


class ApplicationCheckIn(BaseModel):
    """`POST /applications/{id}/checks` — task 7 (3.9b), tz/04 С5: the
    office's veterinary and cadastre checks against outside registries, and
    the paper fallback for when one cannot be reached.

    Two shapes, told apart by `service.add_check` rather than a
    Literal-discriminated union: `check_type` alone calls the live adapter
    (`vet`/`cadastre`); add `source="manual_fallback"` with both `result` and
    `doc_file_id` to record a paper result instead (422 `ERR-VAL-001` if
    either is missing). A paper result is maker-checker (ruling 5, Oybek's
    choice 2026-09-05): it is written with `confirmed_by=None` and is not
    usable until a DIFFERENT reviewer calls `POST .../checks/{id}/confirm`.
    """

    model_config = ConfigDict(extra="forbid")

    check_type: ExternalCheckType
    source: Literal["manual_fallback"] | None = None
    result: CheckResult | None = None
    doc_file_id: uuid.UUID | None = None


class ApplicationDecisionOut(ApplicationOut):
    """The answer to both decision routes: the application's own columns, flat,
    plus where an over-limit application was forwarded to.

    `forwarded_to_organization` is `None` on every real decision — an approval,
    a rejection — and carries the parent organization's id ONLY on a forward,
    where `status` is still `IN_REVIEW` because ruling 9а means the application
    genuinely has not been decided. A client tells the two apart by this field,
    not by guessing from the status.

    Flat rather than `{"application": {...}, "forwarded_to_organization": ...}`,
    the same choice `ApplicationCardOut` made: a client reads `body["status"]`
    in every response this module produces.
    """

    forwarded_to_organization: uuid.UUID | None = None

    @classmethod
    def build(
        cls, application: Any, *, forwarded_to_organization: uuid.UUID | None
    ) -> ApplicationDecisionOut:
        """`ApplicationOut.model_fields` is read rather than the twenty-four
        names retyped — a column added there has to appear here too, and a
        hand-copied list is exactly how the two would drift
        (`ApplicationCardOut.build` is the same shape, for the same reason)."""
        return cls.model_validate(
            {
                **{name: getattr(application, name) for name in ApplicationOut.model_fields},
                "forwarded_to_organization": forwarded_to_organization,
            }
        )


# --- Rulings #179/#182: the benefit claim's verify/reject surface ------------
#
# `benefit_verification.py`/`benefit_verification_router.py` — a sibling of
# `service.py`/`router.py`, the same "second file of the same module, not a
# second module" shape `decision.py` already uses.
#
# Ruling #182 moved this from a central, country-wide office to the leshoz's
# own review (`benefits.verify` now sits on `executor_staff`/`executor_head`):
# the list route `GET /applications/benefit-verifications` and
# `repo`'s country-wide claim-visibility predicate it was built for are GONE —
# a leshoz reviewer works this claim from the application card it already
# reads (`GET /applications/{id}`), the same route everyone else uses. What
# is left here is the single-claim read, `verify` and `reject`, all three
# gated on `benefits.verify` PLUS the application's OWN read rule
# (`service._readable_application` — the same one `GET /applications/{id}`
# applies, so a leshoz reviewer who could not otherwise read this application
# cannot verify its claim either).

# `application_status_history.reason_text`/`applications.decision_basis` both
# cap at 2000 (see `REASON_MAX_LENGTH`/`LEGAL_BASIS_MAX_LENGTH` above);
# `benefit_rejection_reason` is the identical shape — unbounded TEXT column,
# a mandatory human explanation — so it reuses the same ceiling rather than
# inventing a third number that means the same thing.
BENEFIT_REJECTION_REASON_MAX_LENGTH = REASON_MAX_LENGTH


class BenefitClaimDetailOut(ApplicationOut):
    """`GET /applications/benefit-verifications/{id}` — the leshoz reviewer's
    single-claim read, plus its supporting document.

    Deliberately NOT `ApplicationCardOut`: that shape is `service.get_card`'s,
    with `items`/`checks`/`calculation`/`conclusions`/`sla_overdue` this route
    has no use for — the reviewer already sees the whole card through
    `GET /applications/{id}` and reaches this route to decide ONE thing.
    `documents` is the one addition beyond the application's own columns
    (`tz/06` §Льготы: the certificate's supporting file, attached through the
    ordinary document mechanism — see `repo.list_documents`).
    """

    documents: list[ApplicationDocumentOut]

    @classmethod
    def build(cls, application: Any, documents: list[Any]) -> BenefitClaimDetailOut:
        """`ApplicationOut.model_fields` read rather than retyped — the same
        shape `ApplicationCardOut.build`/`ApplicationDecisionOut.build` use,
        for the identical reason: a column added to `ApplicationOut` must not
        need a second edit here to reach this response too."""
        return cls.model_validate(
            {
                **{name: getattr(application, name) for name in ApplicationOut.model_fields},
                "documents": [ApplicationDocumentOut.model_validate(doc) for doc in documents],
            }
        )


class BenefitClaimRejectIn(BaseModel):
    """`POST /applications/benefit-verifications/{id}/reject` — the ONE field
    ruling #179 requires: a reason, MANDATORY (`min_length=1`, the same gap
    `ApplicationRejectIn.legal_basis` closes for the head's own rejection).

    No `pkcs7` here, unlike `ApplicationRejectIn`/`ApplicationApproveIn`: a
    benefit-certificate check is an administrative verification against a
    paper registry, not a decision `tz/04` asks the state to sign — the same
    reasoning `ApplicationReturnIn` states for itself.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, Field(min_length=1, max_length=BENEFIT_REJECTION_REASON_MAX_LENGTH)]
