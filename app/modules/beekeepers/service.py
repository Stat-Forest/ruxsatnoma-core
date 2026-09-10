"""beekeepers service: the Union's register, and the seam `applications`
(wave 2 of this stage) calls on every filing that claims the
`beekeeping_union_member` benefit.

Level 2 (design/01): a self-contained "tool" beside `signatures`/`gis`/
`norms`/`notifications` — it reaches `auth` (level 1) for the OneID-profile
read `lookup_by_pinfl` needs and nothing else below it, and calls no sibling
level-2 module. `applications` (level 3) is the one caller above it, through
`match_certificate` — a PURE function of this register: no application
knowledge, no exceptions for business outcomes (the plan's own words), only
a `MatchResult` the caller interprets.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.schemas import Page, PageParams
from app.modules.audit import service as audit
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.beekeepers import repo
from app.modules.beekeepers.models import Beekeeper
from app.modules.beekeepers.schemas import (
    BeekeeperCreateIn,
    BeekeeperLookupOut,
    BeekeeperOut,
    BeekeeperPatchIn,
    BeekeeperRemoveIn,
)

_AUDITED_FIELDS = (
    "certificate_no",
    "pinfl",
    "passport_series",
    "passport_number",
    "stir",
    "full_name",
    "farm_name",
    "status",
    "removed_reason",
)


def _snapshot(row: Beekeeper) -> dict[str, Any]:
    # Every audited field is a plain string or None — no Decimal/date/UUID to
    # coerce at this JSON boundary (lessons.md), unlike most other modules'
    # own `_snapshot` helpers.
    return {field: getattr(row, field) for field in _AUDITED_FIELDS}


async def _beekeeper_or_404(db: AsyncSession, beekeeper_id: uuid.UUID) -> Beekeeper:
    row = await repo.get_beekeeper(db, beekeeper_id)
    if row is None:
        raise err("ERR-SYS-003", details={"beekeeper": str(beekeeper_id)})
    return row


async def list_beekeepers(
    db: AsyncSession, *, params: PageParams, q: str | None, status: str | None
) -> Page[BeekeeperOut]:
    rows, total = await repo.list_beekeepers(
        db, q=q, status=status, offset=params.offset, limit=params.page_size
    )
    return Page[BeekeeperOut](
        items=[BeekeeperOut.model_validate(row) for row in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


async def create_beekeeper(db: AsyncSession, *, data: BeekeeperCreateIn, actor: User) -> Beekeeper:
    # Stage 10 review, finding 6: the partial unique index is exact-match
    # while every read folds case and whitespace, so two writers racing with
    # `abc-1` and `ABC-1` could both pass the pre-check and every later
    # `match_certificate` would raise `MultipleResultsFound`. Storing the
    # folded form makes the index the arbiter of the SAME rule the reads use.
    data = data.model_copy(update={"certificate_no": _fold(data.certificate_no)})
    if await repo.get_active_by_certificate_no(db, data.certificate_no) is not None:
        raise err(
            "ERR-VAL-001",
            details={"certificate_no": data.certificate_no, "reason": "already exists"},
        )
    row = Beekeeper(
        certificate_no=data.certificate_no,
        pinfl=data.pinfl,
        passport_series=data.passport_series,
        passport_number=data.passport_number,
        stir=data.stir,
        full_name=data.full_name,
        farm_name=data.farm_name,
        status="active",
        created_by=actor.id,
        updated_by=actor.id,
    )
    await repo.add(db, row)
    await audit.log(
        db,
        action="beekeeper.create",
        user_id=actor.id,
        object_type="beekeeper",
        object_id=row.id,
        new_value=_snapshot(row),
    )
    return row


# The NOT NULL columns of the register: a PATCH may leave them alone or
# change them, never blank them (finding 7 above).
_REQUIRED_FIELDS = ("certificate_no", "pinfl", "passport_series", "passport_number", "full_name")


def _fold(certificate_no: str) -> str:
    """The one normalisation of a certificate number — the same the reads
    apply (`repo.get_active_by_certificate_no`)."""
    return certificate_no.strip().upper()


_PATCHABLE_FIELDS = (
    "certificate_no",
    "pinfl",
    "passport_series",
    "passport_number",
    "stir",
    "full_name",
    "farm_name",
)


async def patch_beekeeper(
    db: AsyncSession, *, beekeeper_id: uuid.UUID, data: BeekeeperPatchIn, actor: User
) -> Beekeeper:
    row = await _beekeeper_or_404(db, beekeeper_id)
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        return row
    # Stage 10 review, finding 7: an explicit `null` on a NOT NULL column is
    # a 422 here, never an `IntegrityError` out of `flush()` (a 500).
    nulled = sorted(name for name in _REQUIRED_FIELDS if name in fields and fields[name] is None)
    if nulled:
        raise err("ERR-VAL-001", details={"reason": "null_not_allowed", "fields": nulled})
    if fields.get("certificate_no") is not None:
        fields["certificate_no"] = _fold(fields["certificate_no"])
    before = _snapshot(row)

    new_certificate_no = fields.get("certificate_no")
    if new_certificate_no is not None and new_certificate_no != row.certificate_no:
        existing = await repo.get_active_by_certificate_no(db, new_certificate_no)
        if existing is not None and existing.id != row.id:
            raise err(
                "ERR-VAL-001",
                details={"certificate_no": new_certificate_no, "reason": "already exists"},
            )

    for field in _PATCHABLE_FIELDS:
        if field in fields:
            setattr(row, field, fields[field])
    row.updated_by = actor.id
    await db.flush()
    # `updated_at` is `onupdate=func.now()`: SQLAlchemy fetches it via
    # RETURNING on INSERT but leaves it EXPIRED after a plain UPDATE —
    # `response_model=BeekeeperOut` reading it outside this async context
    # would raise `MissingGreenlet` (lessons.md).
    await db.refresh(row)
    await audit.log(
        db,
        action="beekeeper.update",
        user_id=actor.id,
        object_type="beekeeper",
        object_id=row.id,
        old_value=before,
        new_value=_snapshot(row),
    )
    return row


async def remove_beekeeper(
    db: AsyncSession, *, beekeeper_id: uuid.UUID, data: BeekeeperRemoveIn, actor: User
) -> Beekeeper:
    """Never a DELETE (the plan's own words) — `status='removed'` with a
    mandatory reason, so the row keeps answering `match_certificate` as
    `unknown` for every future filing rather than vanishing."""
    row = await _beekeeper_or_404(db, beekeeper_id)
    if row.status == "removed":
        raise err("ERR-VAL-001", details={"reason": "already removed"})
    before = _snapshot(row)
    row.status = "removed"
    row.removed_reason = data.reason
    row.updated_by = actor.id
    await db.flush()
    # Same `updated_at` expiry as `patch_beekeeper` — see its own comment.
    await db.refresh(row)
    await audit.log(
        db,
        action="beekeeper.remove",
        user_id=actor.id,
        object_type="beekeeper",
        object_id=row.id,
        old_value=before,
        new_value=_snapshot(row),
        basis=data.reason,
    )
    return row


# Uzbek passports: two Latin letters, then seven digits (e.g. "AB1234567").
# OneID's own `passport` field (`integrations.adapters.oneid.OneIdProfile.
# passport`) carries the two glued together as one string; the register's
# form wants them apart. Anything that does not match this shape is left
# UNSPLIT (both None) rather than guessed at — a wrong split would silently
# misfill the form with digits or letters nobody typed, the "hides data
# rather than leaking it" failure direction this project's defects keep
# taking (CLAUDE.md's own status note).
_PASSPORT_RE = re.compile(r"([A-Za-z]{2})\s*([0-9]{7})")


def _split_passport(raw: str | None) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    match = _PASSPORT_RE.fullmatch(raw.strip())
    if match is None:
        return None, None
    return match.group(1).upper(), match.group(2)


async def lookup_by_pinfl(db: AsyncSession, *, pinfl: str, actor: User) -> BeekeeperLookupOut:
    """Ruling #182's "honest auto-fill, as far as we honestly can": the name
    and passport come from a OneID profile snapshot ALREADY on file for this
    PINFL — never typed here, never invented. 404 `ERR-SYS-003` when nobody
    with this PINFL has ever signed in through OneID; full auto-fill for
    everyone else needs the state's person-by-PINFL service, which is not
    something this module can reach (added to the Agency letter, per the
    ruling, not built against nothing)."""
    snapshot = await auth_service.get_oneid_snapshot_by_pinfl(db, pinfl)
    # Stage 10 review, finding 5: a personal-data read keyed on a guessable
    # identifier leaves a row either way — a registrar walking PINFLs one by
    # one is visible in the audit log, hit or miss. `extra`, not `object_id`:
    # the thing looked up is a person, not a register row.
    await audit.log(
        db,
        action="beekeeper.lookup",
        user_id=actor.id,
        object_type="beekeeper",
        result="success" if snapshot is not None else "denied",
        basis=None if snapshot is not None else "not_found",
        extra={"pinfl": pinfl},
    )
    if snapshot is None:
        # Evidence-then-raise (decision #40): the miss is the row worth
        # keeping, and the 404 would roll it back with everything else.
        await db.commit()
        raise err("ERR-SYS-003", details={"pinfl": pinfl})
    full_name = str(snapshot.get("full_name") or "").strip()
    series, number = _split_passport(snapshot.get("passport"))
    return BeekeeperLookupOut(full_name=full_name, passport_series=series, passport_number=number)


@dataclass(frozen=True)
class MatchResult:
    """The seam `applications` (wave 2) calls — a pure function of THIS
    register, nothing about an application. `status`:

      * `matched` — an ACTIVE row exists under this certificate number and
        the given identity (PINFL when given, else STIR) owns it.
        `beekeeper_id` names the row.
      * `unknown` — no ACTIVE row exists under this certificate number (this
        includes a REMOVED row: a removed member reads exactly as if they
        had never been registered, never as a stale match).
      * `not_yours` — an ACTIVE row exists under this certificate number, but
        the given identity does not own it.
    """

    status: Literal["matched", "unknown", "not_yours"]
    beekeeper_id: uuid.UUID | None


async def match_certificate(
    db: AsyncSession, *, certificate_no: str, pinfl: str | None, stir: str | None
) -> MatchResult:
    """`active` rows only, trimmed/case-folded — `repo.get_active_by_
    certificate_no` does both. Identity = PINFL when given, else STIR
    (ruling #182 option а): the caller decides which one to pass by
    `on_behalf` (`self` -> the applicant's own PINFL, `legal` -> the
    applicant's STIR) and this function trusts that choice without knowing
    what it means."""
    row = await repo.get_active_by_certificate_no(db, certificate_no)
    if row is None:
        return MatchResult(status="unknown", beekeeper_id=None)
    if pinfl is not None:
        owns = row.pinfl == pinfl
    elif stir is not None:
        owns = row.stir == stir
    else:
        # The number is real but the caller could name nobody to hold it
        # against — that is "not provably yours", never "no such number".
        owns = False
    if not owns:
        return MatchResult(status="not_yours", beekeeper_id=None)
    return MatchResult(status="matched", beekeeper_id=row.id)
