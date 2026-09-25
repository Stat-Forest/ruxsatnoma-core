"""Pydantic schemas for the beekeepers API.

`Pinfl`/`Stir` here are LOCAL literals, not an import of `admin.users_schemas.
Pinfl`/`admin.schemas.Stir` — this codebase's own convention keeps the same
regex independently in every file that needs it (`auth/models.py`,
`admin/models.py`, `admin/schemas.py`, `admin/users_schemas.py`, and this
module's own `models.py` CHECK) rather than one shared importable symbol, and
a cross-module import here would reach into another module's `schemas.py`
where design/01 rule 2 says only `service` is a door. "Reuse the validators,
don't retype the rules" (the plan's own words) means: use `[0-9]`, never
`\\d` (`\\d` is Unicode-aware in Python but the DB CHECK is ASCII-only —
lessons.md), the exact pattern every other PINFL/STIR field in this schema
already enforces.

`CodeStr`/`NameStr` (`app.core.schemas`), unlike `Pinfl`/`Stir` above, ARE
shared: they are bounds, not domain-specific format rules, and `app.core`
sits below every module (stage 17 t6 folded the module-local `NonBlankStr`
into them — same strip-and-require-non-blank shape, now with an upper bound).
"""

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.schemas import CodeStr, NameStr

Pinfl = Annotated[str, Field(pattern=r"^[0-9]{14}$")]
Stir = Annotated[str, Field(pattern=r"^[0-9]{9}$")]

REMOVED_REASON_MAX_LENGTH = 1000


class BeekeeperOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    certificate_no: str
    pinfl: str
    passport_series: str
    passport_number: str
    stir: str | None
    full_name: str
    farm_name: str | None
    valid_to: date | None
    status: str
    removed_reason: str | None
    created_by: uuid.UUID
    updated_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class BeekeeperCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    certificate_no: CodeStr
    pinfl: Pinfl
    passport_series: CodeStr
    passport_number: CodeStr
    stir: Stir | None = None
    full_name: NameStr
    farm_name: NameStr | None = None
    # Ruling #217: the certificate's term, optional — a registrar copying a
    # certificate without one leaves it blank rather than inventing a date.
    valid_to: date | None = None


class BeekeeperPatchIn(BaseModel):
    """All fields optional; only keys present in the request are touched
    (`exclude_unset=True`), the convention `LegalDocumentPatchIn` established.
    No `status`/`removed_reason` here — removal is its own route
    (`POST /{id}/remove`), never a status value a patch could slip in."""

    model_config = ConfigDict(extra="forbid")

    certificate_no: CodeStr | None = None
    pinfl: Pinfl | None = None
    passport_series: CodeStr | None = None
    passport_number: CodeStr | None = None
    stir: Stir | None = None
    full_name: NameStr | None = None
    farm_name: NameStr | None = None
    valid_to: date | None = None


class BeekeeperRemoveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=REMOVED_REASON_MAX_LENGTH
        ),
    ]


class BeekeeperLookupOut(BaseModel):
    """`GET /beekeepers/lookup` — ruling #182's "honest auto-fill": whatever a
    user who has signed in through OneID left in their own profile snapshot.
    `passport_series`/`passport_number` are nullable — OneID's own `passport`
    field is a single string this seam splits into the two the register's
    form wants, and not every profile carries one (auth/service.py's own
    docstring: "what the provider returns today is evidence, not a
    promise")."""

    full_name: str
    passport_series: str | None
    passport_number: str | None
