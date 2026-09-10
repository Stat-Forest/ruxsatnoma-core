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
"""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Pinfl = Annotated[str, Field(pattern=r"^[0-9]{14}$")]
Stir = Annotated[str, Field(pattern=r"^[0-9]{9}$")]

# Whitespace-only free text has a nonzero length and would otherwise pass a
# bare `Field(min_length=1)` as if it named something real (`auth/schemas.py::
# ApplicantAddressIn`, `permits/schemas.py::DuplicateIn.reason` — the same
# idiom, reused here for the same reason).
NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

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
    status: str
    removed_reason: str | None
    created_by: uuid.UUID
    updated_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class BeekeeperCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    certificate_no: NonBlankStr
    pinfl: Pinfl
    passport_series: NonBlankStr
    passport_number: NonBlankStr
    stir: Stir | None = None
    full_name: NonBlankStr
    farm_name: str | None = None


class BeekeeperPatchIn(BaseModel):
    """All fields optional; only keys present in the request are touched
    (`exclude_unset=True`), the convention `LegalDocumentPatchIn` established.
    No `status`/`removed_reason` here — removal is its own route
    (`POST /{id}/remove`), never a status value a patch could slip in."""

    model_config = ConfigDict(extra="forbid")

    certificate_no: NonBlankStr | None = None
    pinfl: Pinfl | None = None
    passport_series: NonBlankStr | None = None
    passport_number: NonBlankStr | None = None
    stir: Stir | None = None
    full_name: NonBlankStr | None = None
    farm_name: str | None = None


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
