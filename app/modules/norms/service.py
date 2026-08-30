"""Norms, tariffs, parameters, calculations. Everything that decides something.

Tariffs and rule parameters share one lifecycle — draft → published → archived
with maker-checker — so they share one implementation, keyed by a small
descriptor. Norms have their own five-status lifecycle (Task 4)."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.time import business_today
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.auth.models import User
from app.modules.norms import repo
from app.modules.norms.models import RuleParameter, Tariff
from app.modules.norms.permissions import TARIFFS_PUBLISH


@dataclass(frozen=True)
class _Versioned:
    """What differs between a tariff and a parameter: nothing but the table, the
    audit prefix and the columns that make two rows 'the same thing'."""

    model: type[RuleParameter] | type[Tariff]
    audit_object: str

    def key_filters(self, row: Any) -> list[Any]:
        if self.model is RuleParameter:
            return [RuleParameter.code == row.code]
        return [
            Tariff.activity_type_id == row.activity_type_id,
            Tariff.livestock_group.is_not_distinct_from(row.livestock_group),
        ]


PARAMETER = _Versioned(RuleParameter, "rule_parameter")
TARIFF = _Versioned(Tariff, "tariff")


async def _row_or_404(db: AsyncSession, kind: _Versioned, row_id: uuid.UUID) -> Any:
    row = await db.get(kind.model, row_id)
    if row is None:
        raise err("ERR-SYS-003")
    return row


def _snapshot(row: RuleParameter | Tariff) -> dict[str, Any]:
    """JSON-safe view of a versioned row for audit `old_value`/`new_value`
    (lesson: a JSONB column fed by the stock `json.dumps` rejects
    Decimal/date/UUID — `DomainError`'s own response has the same gap, but
    `audit_log` is the one at risk here since every field below can be any
    of those three). Built by walking the mapped columns rather than a
    hand-typed field list, since `RuleParameter` and `Tariff` share no field
    names beyond the versioned-lifecycle ones."""
    data: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, uuid.UUID | Decimal):
            value = str(value)
        elif isinstance(value, datetime | date):
            value = value.isoformat()
        data[column.name] = value
    return data


async def _holds_tariffs_publish(db: AsyncSession, actor: User) -> bool:
    """Holds `norms.tariffs.publish`, or is the superuser that passes every
    permission gate (decision #41 ruling 2) — the same two-branch shape
    `gis.service._may_manage_layers`/`admin.users_service._may_manage` use for
    a rule INSIDE a handler, as opposed to a `require_permission` dependency
    on the route itself."""
    if await auth_repo.role_code(db, actor) == SUPERUSER_ROLE:
        return True
    return TARIFFS_PUBLISH in await auth_repo.permission_codes(db, actor)


async def create_versioned(db: AsyncSession, kind: _Versioned, payload: Any, *, actor: User) -> Any:
    """A fresh draft. No maker-checker or period check yet — those only bind a
    PUBLISHED row (ruling 10), so two drafts (or a draft and a published row)
    may freely overlap until someone tries to publish one of them."""
    row = kind.model(**payload.model_dump(), status="draft", created_by=actor.id)
    db.add(row)
    await db.flush()
    # A fixed-scale NUMERIC (Tariff.coefficient is numeric(12,6)) round-trips at
    # the COLUMN's precision, not the caller's (lesson) — Postgres pads "1.5" to
    # "1.500000" on write, but INSERT's implicit RETURNING only refreshes
    # server-generated columns, not ones we supplied a value for ourselves, so
    # without this the create response would echo the caller's own unpadded
    # string instead of the value every other read of this row will show.
    await db.refresh(row)
    await audit.log(
        db,
        action=f"{kind.audit_object}.create",
        user_id=actor.id,
        object_type=kind.audit_object,
        object_id=row.id,
        new_value=_snapshot(row),
    )
    return row


async def update_versioned(
    db: AsyncSession, kind: _Versioned, row_id: uuid.UUID, patch: Any, *, actor: User
) -> Any:
    """Draft-only edit. A published row is immutable except through
    publish/archive (`test_a_published_parameter_cannot_be_edited`) — a stray
    PATCH must never silently move a rate a calculation may already have been
    computed against."""
    row = await _row_or_404(db, kind, row_id)
    if row.status != "draft":
        raise err("ERR-NORM-005", details={"reason": "not_draft"})
    before = _snapshot(row)
    for field, value in patch.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    await db.flush()
    # `updated_at` is `onupdate=func.now()`: an UPDATE leaves it expired (unlike
    # an INSERT, which gets it back via RETURNING), so reading it in `_snapshot`
    # below without a refresh raises MissingGreenlet (lesson).
    await db.refresh(row)
    await audit.log(
        db,
        action=f"{kind.audit_object}.update",
        user_id=actor.id,
        object_type=kind.audit_object,
        object_id=row.id,
        old_value=before,
        new_value=_snapshot(row),
    )
    return row


async def publish_versioned(
    db: AsyncSession, kind: _Versioned, row_id: uuid.UUID, *, actor: User
) -> tuple[Any, list[dict[str, str]]]:
    """Maker-checker publication (ruling 10) with the retroactivity warning
    (ruling 11). Three refusals, all 409 ERR-NORM-005 with a `reason`:
    the row is not a draft, the actor is its own maker, or a published row
    already covers part of the period."""
    row = await _row_or_404(db, kind, row_id)
    if row.status != "draft":
        raise err("ERR-NORM-005", details={"reason": "not_draft"})
    if row.created_by is not None and row.created_by == actor.id:
        raise err("ERR-NORM-005", details={"reason": "not_maker_checker"})
    if await repo.published_overlaps(db, kind.model, row, kind.key_filters(row)):
        raise err("ERR-NORM-005", details={"reason": "period_overlap"})

    row.status = "published"
    row.approved_by = actor.id
    warnings: list[dict[str, str]] = []
    retroactive = row.effective_from < business_today()
    if retroactive:
        warnings.append(
            {
                "code": "RI-04",
                "message": (
                    "Effective date is in the past; existing calculations are not recomputed"
                ),
            }
        )
    await db.flush()
    await audit.log(
        db,
        action=f"{kind.audit_object}.publish",
        user_id=actor.id,
        object_type=kind.audit_object,
        object_id=row.id,
        new_value={"status": "published", "retroactive": retroactive},
    )
    return row, warnings


async def archive_versioned(
    db: AsyncSession, kind: _Versioned, row_id: uuid.UUID, *, actor: User
) -> Any:
    """Published or draft -> archived (idempotent: archiving an already-archived
    row is a no-op, mirroring `admin.service.archive_classifier_item`).

    Unlike `publish_versioned`, this has no `created_by` to compare against —
    archiving is a single-actor action, not a handoff between two drafts of
    the same row, so there is nothing to tell a maker apart from a checker BY
    IDENTITY here. That means the router's shared
    `require_any_permission(TARIFFS_PUBLISH, TARIFFS_MANAGE)` gate (widened for
    the same reason `publish_parameter`'s is — `refs_router.py`'s module
    docstring — so a maker reaches a domain answer instead of a bare 403) is
    not enough on its own: taking a PUBLISHED row out of force is exactly the
    one-person change to the numbers in force that
    maker-checker exists to prevent, so it needs the checker's own permission,
    checked here. Archiving a DRAFT stays available to a maker alone — a maker
    must be able to discard their own draft without pulling in a second person.

    An open `effective_to` is closed at `business_today() - 1 day`, clamped so
    it can never precede `effective_from` — the exact clamp
    `admin.service.archive_classifier_item` uses, for the exact same reason: a
    row whose `effective_from` is today or later would otherwise compute an
    end before its own start and fail the `period_valid` CHECK at flush
    instead of archiving cleanly."""
    row = await _row_or_404(db, kind, row_id)
    if row.status == "archived":
        return row
    if row.status == "published" and not await _holds_tariffs_publish(db, actor):
        raise err("ERR-ACL-001")
    before = _snapshot(row)
    row.status = "archived"
    row.effective_to = row.effective_to or max(
        row.effective_from, business_today() - timedelta(days=1)
    )
    await db.flush()
    # Same `onupdate=func.now()` expiry as `update_versioned` (lesson): refresh
    # before `_snapshot` reads `updated_at` below.
    await db.refresh(row)
    await audit.log(
        db,
        action=f"{kind.audit_object}.archive",
        user_id=actor.id,
        object_type=kind.audit_object,
        object_id=row.id,
        old_value=before,
        new_value=_snapshot(row),
    )
    return row
