"""Norms, tariffs, parameters, calculations. Everything that decides something.

Tariffs and rule parameters share one lifecycle — draft → published → archived
with maker-checker — so they share one implementation, keyed by a small
descriptor. Norms have their own five-status lifecycle (Task 4).

Public surface for levels 4+ (applications 3.9, payments 3.10, permits 3.11):
preview(), save_calculation(), effective_norm(), run_checks(),
latest_calculation(), LOAD_PROVIDERS — see the dedicated section near the end
of this module for what a caller at those levels may and may not do with
them."""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.abac import Zone, zone_of
from app.core.errors import err
from app.core.models import MediaFile
from app.core.time import business_today
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import Organization
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.norms import calculator, checks, repo
from app.modules.norms import params as norm_params
from app.modules.norms.models import Calculation, Norm, RuleParameter, Tariff
from app.modules.norms.permissions import TARIFFS_PUBLISH
from app.modules.norms.schemas import CalculationIn, NormIn, NormPatch


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


def _snapshot(row: RuleParameter | Tariff | Norm | Calculation) -> dict[str, Any]:
    """JSON-safe view of a versioned row for audit `old_value`/`new_value`
    (lesson: a JSONB column fed by the stock `json.dumps` rejects
    Decimal/date/UUID — `DomainError`'s own response has the same gap, but
    `audit_log` is the one at risk here since every field below can be any
    of those three). Built by walking the mapped columns rather than a
    hand-typed field list, since `RuleParameter`/`Tariff`/`Norm` share no field
    names beyond the versioned-lifecycle ones. `Norm`'s own `season`/`rotation`
    JSONB dicts need no extra handling here — they are already JSON-safe.
    Reused as-is for `Calculation` (Task 7): its `input_snapshot`/`breakdown`
    JSONB columns are already fully `jsonable()`-safe by the time a row is
    built, so this walk's per-column Decimal/date/UUID coercion never has
    anything left to do for those two — it only matters for the plain scalar
    columns beside them (`amount`, `used_sb`, `created_at`, ...)."""
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


async def _assert_benefit_codes(db: AsyncSession, payload: Any) -> None:
    """Ruling 20 defines `benefit_modifiers`' KEYS as `benefit_categories`
    classifier item codes, but nothing checked them (I7, final review): a typo
    created a benefit nobody can claim, or one nobody intended. Validated the
    same way `activity_type_id` already is here, through `admin.repo` rather
    than a direct `classifier_items` query (module boundary, 'Reference data').

    `RuleParameterIn` has no such field, so `getattr(..., None)` skips this
    for a parameter without a per-kind branch — the same shape the
    `activity_type_id` guard below uses.

    Resolved against the items active TODAY: a tariff dated into the future is
    still drafted against the benefit list as it stands, and a category that
    has been withdrawn should not be attachable to a new rate.

    What this does NOT and cannot answer: whether the applicant who later
    CLAIMS one of these codes is entitled to it. That is the application's
    fact, not the tariff's — stage 3.9 (see `schemas.CalculationIn`)."""
    modifiers = getattr(payload, "benefit_modifiers", None)
    if not modifiers:
        return
    classifier = await admin_repo.get_classifier_by_code(db, "benefit_categories")
    known = (
        {item.code for item in await admin_repo.list_classifier_items(db, classifier.id)}
        if classifier is not None
        else set()
    )
    unknown = sorted(set(modifiers) - known)
    if unknown:
        raise err("ERR-VAL-001", details={"reason": "unknown_benefit_category", "codes": unknown})


async def create_versioned(db: AsyncSession, kind: _Versioned, payload: Any, *, actor: User) -> Any:
    """A fresh draft. No maker-checker or period check yet — those only bind a
    PUBLISHED row (ruling 10), so two drafts (or a draft and a published row)
    may freely overlap until someone tries to publish one of them."""
    # `TariffIn.activity_type_id` is a real FK; `RuleParameterIn` has no such
    # field at all, so `getattr(..., None)` skips this for a parameter without
    # a per-kind branch. Unvalidated, a garbage id reached `flush()` and
    # surfaced as an uncaught `IntegrityError` -> ERR-SYS-001/500 (found in
    # task-4 review, `POST /tariffs`) — the same defect class this stage
    # already guards against for `period_overlap`.
    activity_type_id = getattr(payload, "activity_type_id", None)
    if activity_type_id is not None:
        known = await admin_repo.list_activity_types(db)
        if not any(activity.id == activity_type_id for activity in known):
            raise err("ERR-VAL-001", details={"reason": "unknown_activity_type"})
    await _assert_benefit_codes(db, payload)
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
    # A PATCH can set `benefit_modifiers` too, so the same guard applies here —
    # validating only on create would leave the hole open one HTTP verb over.
    await _assert_benefit_codes(db, patch)
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
    already covers part of the period — plus one 403 for an actor who is not a
    checker at all.

    That 403 is the same shape `archive_versioned` below carries, and it is
    here for the same reason (C1, final review). The route dependency accepts
    EITHER tariff permission on purpose, so a maker gets the domain
    `not_maker_checker` answer rather than a bare 403 — but the identity check
    alone is not the control: two DIFFERENT makers, neither of them a checker,
    satisfy `created_by != actor` and the DB `maker_checker` CHECK both, and a
    migration-seeded row (`created_by IS NULL`) skips the identity check
    outright, so a single `TARIFFS_MANAGE` holder could have put the ten
    provisional `coef_sb:*` drafts into force alone. Publication is what sets
    the numbers in force, so it takes the checker's own permission, checked
    HERE — after the two domain refusals, so a maker publishing their own
    draft still sees why."""
    row = await _row_or_404(db, kind, row_id)
    if row.status != "draft":
        raise err("ERR-NORM-005", details={"reason": "not_draft"})
    if row.created_by is not None and row.created_by == actor.id:
        raise err("ERR-NORM-005", details={"reason": "not_maker_checker"})
    if not await _holds_tariffs_publish(db, actor):
        raise err("ERR-ACL-001")
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


# --- Task 4: the norm itself — Draft -> Review -> Approved -> Published ->
# Archived (VMQ 689). The lifecycle mirrors 3.6a's contour versions, but who
# may PUBLISH depends on the `norms_publish_scope` runtime setting (ruling 16)
# rather than a fixed role, and MaxSB is computed and frozen at that moment
# (ruling 17) from the contour's PUBLISHED version area — never
# `declared_area_ha` — and the parameters in force on the norm's own
# `effective_from`, not today.

# (from, to) pairs that exist at all. `publish` additionally needs the central
# permission, checked on the route; the send-backs mirror 3.6a's contour versions.
NORM_TRANSITIONS = {
    ("draft", "review"),
    ("review", "approved"),
    ("review", "draft"),  # sent back by the reviewer
    ("approved", "published"),
    ("approved", "review"),  # sent back by the central office
    ("published", "archived"),
    ("draft", "archived"),
}


def _assert_transition(norm: Norm, target: str) -> None:
    if (norm.status, target) not in NORM_TRANSITIONS:
        raise err("ERR-NORM-005", details={"reason": "bad_transition"})


def _assert_transition_from(norm: Norm, source: str, target: str) -> None:
    """`_assert_transition` alone is ambiguous wherever two source states share
    one target (lesson): `NORM_TRANSITIONS` allows `review` from BOTH `draft`
    (`submit_norm_review`, `NORMS_MANAGE`) and `approved`
    (`return_norm_to_review`, `NORMS_PUBLISH`) — a bare "may this become
    review?" would let either route drive the OTHER's edge under the wrong
    permission. `archived` has the same two-source shape (`published`,
    `draft`) but only ONE route/permission (`NORMS_APPROVE`) ever reaches it,
    so `archive_norm` stays on the plain `_assert_transition` below — there is
    nothing to disambiguate when a single handler owns both edges."""
    if norm.status != source:
        raise err("ERR-NORM-005", details={"reason": "bad_transition"})
    _assert_transition(norm, target)


async def _norm_or_404(db: AsyncSession, norm_id: uuid.UUID) -> Norm:
    norm = await db.get(Norm, norm_id)
    if norm is None:
        raise err("ERR-SYS-003")
    return norm


def _organization_in_zone(zone: Zone, org: Organization) -> bool:
    """Per-row equivalent of `zone_filter`'s SQL for ONE organization row — a
    LOCAL copy of `gis.service`'s own private helper of the same name and
    identical logic. The module boundary (CLAUDE.md: norms reaches gis only
    through `gis.service`) rules out importing it directly: it is not part of
    that module's declared public surface for level-3 callers
    (`published_version`/`list_contours`/`run_checks`/`contour_organization`)."""
    if zone.region_id is not None and zone.region_id != org.region_id:
        return False
    if zone.district_id is not None and zone.district_id != org.district_id:
        return False
    if zone.organization_id is not None and zone.organization_id != org.id:
        return False
    return True


async def _assert_norm_zone(db: AsyncSession, actor: User, contour_id: uuid.UUID) -> uuid.UUID:
    """Resolves which leshoz a contour belongs to through `gis.service`
    (never `gis.repo`, never a direct query of `contours` — module boundary)
    and refuses an actor whose zone does not cover it. EVERY write path below
    calls this — create (which has no `Norm` row yet, so it takes the
    request's own `contour_id`), update, the two submit/send-back pairs,
    approve, publish and archive — because zone scoping is not a permission
    check (lesson) and a per-endpoint guard is not a root fix when several
    actions share this same precondition (lesson)."""
    organization_id = await gis_service.contour_organization(db, contour_id)
    if organization_id is None:
        raise err("ERR-SYS-003")
    zone = zone_of(actor)
    if zone != Zone(None, None, None):
        org = await admin_repo.get_organization(db, organization_id)
        if org is None or not _organization_in_zone(zone, org):
            raise err("ERR-ACL-002")
    return organization_id


async def _assert_may_publish(db: AsyncSession, actor: User) -> None:
    """Ruling 16: VMQ 689 puts forest-pasture norms in force centrally by
    default. `norms_publish_scope` is the runtime override — `"central"` (the
    default) means only a zone-free actor may publish; `"leshoz"` extends that
    to a zone-scoped raҳbar too (still subject to `_assert_norm_zone`'s own
    check that it is THEIR OWN leshoz's norm — this function answers a
    different question: may a leshoz actor publish ANY norm at all, right
    now). Independent of the route's own `NORMS_PUBLISH` permission (may this
    ROLE publish at all) — a zone-free actor never needs to consult the
    setting, the same short-circuit `_assert_norm_zone` itself uses."""
    if zone_of(actor) == Zone(None, None, None):
        return
    scope = await settings_store.get_str(db, "norms_publish_scope")
    if scope != "leshoz":
        raise err("ERR-ACL-002", details={"reason": "central_publication_required"})


async def _assert_doc_active(db: AsyncSession, file_id: uuid.UUID, *, reason: str) -> None:
    """An EXISTENCE check, not a validity check (lesson) — confirms a
    `media_files` row exists and is not archived; nothing about whether it
    actually authorises this norm. `MediaFile` is a core (level 0) model, so
    reading it directly here crosses no module boundary. Mirrors
    `gis.service._assert_approval_doc_active`, a private helper of a sibling
    module this one may not import."""
    file = await db.get(MediaFile, file_id)
    if file is None or file.status != "active":
        raise err("ERR-VAL-001", details={"reason": reason})


# Ruling 12: `ActivePermitsSB` is a registered seam, and it says so — the same
# idiom as 3.6a's `OCCUPANCY_PROVIDERS` and `files.ACCESS_CHECKS`. Nothing
# registers here until 3.11 wires the real permit-load query; until then the
# load is always zero and `load_source` says "none" so a caller can never
# mistake the placeholder for a measurement.
LoadProvider = Callable[[AsyncSession, uuid.UUID, date, date], Awaitable[Decimal]]
LOAD_PROVIDERS: list[LoadProvider] = []


async def committed_load_sb(
    db: AsyncSession, contour_id: uuid.UUID, period_from: date, period_to: date
) -> tuple[Decimal, str]:
    """Conditional heads already committed on this contour for an overlapping
    period. Empty until 3.11 registers a provider — and the source string says
    so, so a caller can never read the placeholder as a measurement."""
    if not LOAD_PROVIDERS:
        return Decimal("0"), "none"
    total = Decimal("0")
    for provider in LOAD_PROVIDERS:
        total += await provider(db, contour_id, period_from, period_to)
    return total, "permits"


async def create_norm(db: AsyncSession, payload: NormIn, *, actor: User) -> Norm:
    """A fresh draft (VMQ 689: the leshoz GIS specialist drafts from a
    geobotanical survey). No period check yet — like `create_versioned`, that
    only binds a PUBLISHED norm — and the survey document is optional here,
    required only to submit for review (`submit_norm_review`)."""
    await _assert_norm_zone(db, actor, payload.contour_id)
    known = await admin_repo.list_activity_types(db)
    if not any(activity.id == payload.activity_type_id for activity in known):
        raise err("ERR-VAL-001", details={"reason": "unknown_activity_type"})
    if payload.geobotanic_doc_id is not None:
        # An EXISTENCE check (lesson), the same one `approve_norm` already
        # applies to `approval_doc_id` — reusing `_assert_doc_active` rather
        # than a second, near-identical document check.
        await _assert_doc_active(db, payload.geobotanic_doc_id, reason="geobotanic_doc_required")
    if payload.effective_to is not None and payload.effective_to < payload.effective_from:
        # Caught here, not by the `period_valid` DB CHECK (mirrors
        # admin.service.add_classifier_item's own reasoning): an
        # IntegrityError has no handler in main.py and would surface as
        # ERR-SYS-001/500.
        raise err("ERR-VAL-001", details={"reason": "effective_to_before_from"})
    # `by_alias=True` so `SeasonWindow.from_` is stored as `"from"` — the shape
    # `checks._in_window` reads and every existing row already has (I5). No
    # other field on this payload carries an alias, so nothing else moves.
    norm = Norm(**payload.model_dump(by_alias=True), status="draft", created_by=actor.id)
    db.add(norm)
    await db.flush()
    # A caller-supplied fixed-scale NUMERIC (yield_c_per_ha) round-trips at the
    # column's own precision, not the caller's (lesson) — refresh before the
    # response serializes it.
    await db.refresh(norm)
    await audit.log(
        db,
        action="norm.create",
        user_id=actor.id,
        object_type="norm",
        object_id=norm.id,
        new_value=_snapshot(norm),
    )
    return norm


async def get_norm(db: AsyncSession, norm_id: uuid.UUID) -> Norm:
    return await _norm_or_404(db, norm_id)


async def update_norm(
    db: AsyncSession, norm_id: uuid.UUID, patch: NormPatch, *, actor: User
) -> Norm:
    """Draft or review only — approved and beyond are a fact of record."""
    norm = await _norm_or_404(db, norm_id)
    await _assert_norm_zone(db, actor, norm.contour_id)
    if norm.status not in ("draft", "review"):
        raise err("ERR-NORM-005", details={"reason": "not_draft"})
    fields = patch.model_dump(exclude_unset=True, by_alias=True)
    if fields.get("geobotanic_doc_id") is not None:
        await _assert_doc_active(db, fields["geobotanic_doc_id"], reason="geobotanic_doc_required")
    effective_from = fields.get("effective_from", norm.effective_from)
    effective_to = fields.get("effective_to", norm.effective_to)
    if effective_to is not None and effective_to < effective_from:
        raise err("ERR-VAL-001", details={"reason": "effective_to_before_from"})
    before = _snapshot(norm)
    for field, value in fields.items():
        setattr(norm, field, value)
    await db.flush()
    await db.refresh(norm)
    await audit.log(
        db,
        action="norm.update",
        user_id=actor.id,
        object_type="norm",
        object_id=norm.id,
        old_value=before,
        new_value=_snapshot(norm),
    )
    return norm


async def submit_norm_review(db: AsyncSession, norm_id: uuid.UUID, *, actor: User) -> Norm:
    """draft -> review, `NORMS_MANAGE` (the specialist hands their own draft
    to the raҳbar). VMQ 689: a norm comes out of a geobotanical survey, so one
    with no survey file on record must not reach a reviewer."""
    norm = await _norm_or_404(db, norm_id)
    await _assert_norm_zone(db, actor, norm.contour_id)
    _assert_transition_from(norm, "draft", "review")
    if norm.geobotanic_doc_id is None:
        raise err("ERR-VAL-001", details={"reason": "geobotanic_doc_required"})
    norm.status = "review"
    await db.flush()
    await db.refresh(norm)
    await audit.log(
        db,
        action="norm.submit_review",
        user_id=actor.id,
        object_type="norm",
        object_id=norm.id,
        old_value={"status": "draft"},
        new_value={"status": "review"},
    )
    return norm


async def return_norm_to_draft(db: AsyncSession, norm_id: uuid.UUID, *, actor: User) -> Norm:
    """review -> draft, `NORMS_MANAGE` — the specialist takes their own
    submission back to the bench (mirrors `gis.service.return_to_draft`)."""
    norm = await _norm_or_404(db, norm_id)
    await _assert_norm_zone(db, actor, norm.contour_id)
    _assert_transition_from(norm, "review", "draft")
    norm.status = "draft"
    await db.flush()
    await db.refresh(norm)
    await audit.log(
        db,
        action="norm.return_to_draft",
        user_id=actor.id,
        object_type="norm",
        object_id=norm.id,
        old_value={"status": "review"},
        new_value={"status": "draft"},
    )
    return norm


async def approve_norm(
    db: AsyncSession, norm_id: uuid.UUID, approval_doc_id: uuid.UUID, *, actor: User
) -> Norm:
    """review -> approved, `NORMS_APPROVE` (the raҳbar — role code
    `leadership`, never the specialist who drafted it). `approval_doc_id` is
    mandatory here, mirroring `gis.service.approve_version`: the
    `published_needs_doc` CHECK only fires at publish, but the basis document
    must be on record before the raҳbar's own approval means anything."""
    norm = await _norm_or_404(db, norm_id)
    await _assert_norm_zone(db, actor, norm.contour_id)
    _assert_transition(norm, "approved")
    await _assert_doc_active(db, approval_doc_id, reason="approval_doc_required")
    norm.status = "approved"
    norm.approval_doc_id = approval_doc_id
    norm.approved_by = actor.id
    await db.flush()
    await db.refresh(norm)
    await audit.log(
        db,
        action="norm.approve",
        user_id=actor.id,
        object_type="norm",
        object_id=norm.id,
        old_value={"status": "review"},
        new_value={"status": "approved", "approval_doc_id": str(approval_doc_id)},
    )
    return norm


async def return_norm_to_review(db: AsyncSession, norm_id: uuid.UUID, *, actor: User) -> Norm:
    """approved -> review, `NORMS_PUBLISH` — the central office sends an
    approved norm back for rework instead of publishing it."""
    norm = await _norm_or_404(db, norm_id)
    await _assert_norm_zone(db, actor, norm.contour_id)
    _assert_transition_from(norm, "approved", "review")
    norm.status = "review"
    await db.flush()
    await db.refresh(norm)
    await audit.log(
        db,
        action="norm.return_to_review",
        user_id=actor.id,
        object_type="norm",
        object_id=norm.id,
        old_value={"status": "approved"},
        new_value={"status": "review"},
    )
    return norm


async def publish_norm(db: AsyncSession, norm_id: uuid.UUID, *, actor: User) -> Norm:
    """approved → published. Computes and freezes MaxSB from the contour's
    PUBLISHED version area (ruling 17) and the parameters in force on the norm's
    own `effective_from` — not on today, so a norm dated into the future uses the
    numbers that will apply to it."""
    norm = await _norm_or_404(db, norm_id)
    await _assert_norm_zone(db, actor, norm.contour_id)
    _assert_transition(norm, "published")
    await _assert_may_publish(db, actor)
    if norm.approval_doc_id is None:
        raise err("ERR-VAL-001", details={"reason": "approval_doc_required"})
    # I2 (final review): `yield_c_per_ha` is optional on the model and in
    # `NormIn`, and `max_sb` below is computed only when it is present — so a
    # GRAZING norm published without it switched the VMQ 689 limit off
    # entirely: `_norm_check` passed (a norm exists), `_limit_check` reported
    # `skipped`/`no_limit`, and `save_calculation` accepted any herd size at
    # all. Ruling 13 (a norm is required only where the law imposes a limit)
    # and ruling 17 (`max_sb` is frozen at publication) together mean a
    # PUBLISHED grazing norm must carry its limit. Every other activity is
    # legitimately yield-free.
    if await _resolve_activity_code(db, norm.activity_type_id) == calculator.GRAZING:
        if norm.yield_c_per_ha is None:
            raise err("ERR-VAL-001", details={"reason": "yield_required"})
    if await repo.published_overlaps(
        db,
        Norm,
        norm,
        [Norm.contour_id == norm.contour_id, Norm.activity_type_id == norm.activity_type_id],
    ):
        raise err("ERR-NORM-005", details={"reason": "period_overlap"})

    version = await gis_service.published_version(db, norm.contour_id)
    if version is None:
        raise err("ERR-NORM-005", details={"reason": "no_published_contour"})

    if norm.yield_c_per_ha is not None:
        # `calculator.max_sb` is the SAME formula `norms.calculator.calculate`
        # would use for a fresh preview — freezing it here and reading it back
        # unchanged from `Norm.max_sb` everywhere else (lesson: two copies of
        # the feed-stock formula is exactly the defect this stage exists to
        # prevent). `load_limit_params` raises `ERR-NORM-004` naming whichever
        # of `safety_reserve`/`sb_feed_norm`/`season_share` is missing the
        # moment `calculator.max_sb` reads it, not before.
        limit_params = await norm_params.load_limit_params(db, on_date=norm.effective_from)
        norm.max_sb = calculator.max_sb(
            area_ha=version.area_ha, yield_c_per_ha=norm.yield_c_per_ha, params=limit_params
        )
    norm.status = "published"
    norm.published_at = datetime.now(UTC)
    await db.flush()
    await audit.log(
        db,
        action="norm.publish",
        user_id=actor.id,
        object_type="norm",
        object_id=norm.id,
        new_value={"status": "published", "max_sb": norm.max_sb, "area_ha": str(version.area_ha)},
    )
    return norm


async def archive_norm(db: AsyncSession, norm_id: uuid.UUID, *, actor: User) -> Norm:
    """draft or published -> archived, `NORMS_APPROVE`. Closes an open
    `effective_to` at `business_today() - 1 day` (clamped so it can never
    precede `effective_from`), mirroring `archive_versioned`/
    `admin.service.archive_classifier_item`."""
    norm = await _norm_or_404(db, norm_id)
    await _assert_norm_zone(db, actor, norm.contour_id)
    _assert_transition(norm, "archived")
    before = _snapshot(norm)
    norm.status = "archived"
    norm.effective_to = norm.effective_to or max(
        norm.effective_from, business_today() - timedelta(days=1)
    )
    await db.flush()
    await db.refresh(norm)
    await audit.log(
        db,
        action="norm.archive",
        user_id=actor.id,
        object_type="norm",
        object_id=norm.id,
        old_value=before,
        new_value=_snapshot(norm),
    )
    return norm


# --- Task 7: preview and saved calculations. `_compute` is the ONE path both
# `preview` and `save_calculation` run through, so the arithmetic behind them
# can never become two implementations that quietly drift apart — they differ
# only in what they do with the result: a preview reports it, a save commits
# it (or refuses to).


async def _resolve_activity_code(db: AsyncSession, activity_type_id: uuid.UUID) -> str:
    """Same existence guard `create_norm`/`create_versioned` already apply to
    this FK, plus the CODE `CalcRequest.activity_code` needs — resolved
    through `admin.repo`, never a direct `activity_types` query (module
    boundary, 'Reference data')."""
    known = await admin_repo.list_activity_types(db)
    activity = next((a for a in known if a.id == activity_type_id), None)
    if activity is None:
        raise err("ERR-VAL-001", details={"reason": "unknown_activity_type"})
    return activity.code


async def _build_request_and_snapshot(
    db: AsyncSession, payload: CalculationIn
) -> tuple[calculator.CalcRequest, calculator.ParamSnapshot]:
    """Builds the request and loads the snapshot — the one place that does, so
    `_compute` (a priced answer: preview/save) and `run_checks` (a reviewer's
    screen, unpriced) can never see two different requests for what is
    supposed to be the same input.

    `contour_id` is checked for existence the way `_assert_norm_zone` checks
    it for a norm — via `gis_service.contour_organization`, never a direct
    `contours` query (module boundary) — but WITHOUT a zone check: an
    applicant has no leshoz of their own, and pricing any contour nationwide
    is exactly what `tz/04` С3 asks this route to do. `area_ha` is recorded
    for `input_snapshot` from the contour's own PUBLISHED area (never a
    caller-declared figure, same principle as `contour_versions.area_ha`
    itself) — `Decimal('0')` when the contour has none, since it plays no
    part in `Amount` either way (calculator.py's own docstring)."""
    activity_code = await _resolve_activity_code(db, payload.activity_type_id)
    if await gis_service.contour_organization(db, payload.contour_id) is None:
        raise err("ERR-SYS-003")
    version = await gis_service.published_version(db, payload.contour_id)
    area_ha = version.area_ha if version is not None else Decimal("0")

    request = calculator.CalcRequest(
        activity_code=activity_code,
        on_date=business_today(),
        period_from=payload.period_from,
        period_to=payload.period_to,
        area_ha=area_ha,
        items=tuple(
            calculator.LivestockItem(item.livestock_code, item.count) for item in payload.items
        ),
        quantity=payload.quantity,
        benefit_code=payload.benefit_code,
    )
    snapshot = await norm_params.load_snapshot(
        db,
        request=request,
        contour_id=payload.contour_id,
        activity_type_id=payload.activity_type_id,
    )
    return request, snapshot


async def _compute(
    db: AsyncSession, payload: CalculationIn
) -> tuple[
    calculator.CalcRequest,
    calculator.ParamSnapshot,
    calculator.CalcResult,
    list[checks.CheckResult],
]:
    """`_build_request_and_snapshot`, then computes the amount and runs every
    admissibility check against that SAME request/snapshot pair, with the
    amount's own `used_sb` fed into the limit check — `preview`/
    `save_calculation`'s shared arithmetic.

    Does NOT itself guard `period_to < period_from`: `checks.run_checks`
    already rejects a reversed or oversized period before any check runs, and
    duplicating that guard here would be exactly the class of bug the 'a
    per-endpoint guard is not a root fix' lesson warns about — this module IS
    that one shared entry point, so the guard stays where it already lives."""
    request, snapshot = await _build_request_and_snapshot(db, payload)
    result = calculator.calculate(request, snapshot)
    check_results = await checks.run_checks(
        db,
        request=request,
        contour_id=payload.contour_id,
        activity_type_id=payload.activity_type_id,
        snapshot=snapshot,
        used_sb=result.used_sb,
    )
    return request, snapshot, result, check_results


async def preview(db: AsyncSession, *, payload: CalculationIn, actor: User) -> dict[str, Any]:
    """Compute and report. Changes nothing, so a failed CHECK is data in the
    response, not an HTTP error (design/03) — but a broken INPUT still is one:
    a missing parameter (ERR-NORM-004) or an unknown benefit code (ERR-VAL-001)
    mean the answer would be a fiction, and a fiction is worse than a 422.

    `actor` is unused today (no permission or zone rule beyond the route's own
    `get_current_user` — an applicant prices their own request, tz/04 С3) and
    kept only for signature symmetry with `save_calculation`; nothing here is
    audited either, matching `test_a_preview_writes_nothing`'s own name."""
    _, _, result, check_results = await _compute(db, payload)
    return calculator.jsonable(
        {
            "amount": result.amount,
            "used_sb": result.used_sb,
            "max_sb": result.max_sb,
            "remaining_sb": result.remaining_sb,
            "breakdown": result.breakdown,
            "rule_code_version": result.rule_code_version,
            "checks": check_results,
            "input_snapshot": result.input_snapshot,
        }
    )


async def save_calculation(db: AsyncSession, *, payload: CalculationIn, actor: User) -> Calculation:
    """The same arithmetic, persisted. Here a blocking check DOES refuse:
    `first_blocking_error` raises ERR-NORM-001/002/003 with the whole check
    list in `details`. A saved calculation is what an invoice is built from
    (3.10) — always a NEW row (`calculations` is append-only, migration 0011):
    a recalculation for the same `application_id` is a second insert, never
    an UPDATE (ruling 21)."""
    _, _, result, check_results = await _compute(db, payload)
    blocking = checks.first_blocking_error(check_results)
    if blocking is not None:
        raise blocking
    row = Calculation(
        application_id=payload.application_id,
        contour_id=payload.contour_id,
        activity_type_id=payload.activity_type_id,
        rule_code_version=result.rule_code_version,
        input_snapshot=result.input_snapshot,
        used_sb=result.used_sb,
        max_sb=result.max_sb,
        remaining_sb=result.remaining_sb,
        amount=result.amount,
        breakdown=result.breakdown,
        created_by=actor.id,
    )
    db.add(row)
    await db.flush()
    # A fixed-scale NUMERIC round-trips at the COLUMN's own precision, not the
    # calculator's (lesson) — refresh before either the audit snapshot below or
    # the response reads amount/used_sb/remaining_sb back.
    await db.refresh(row)
    await audit.log(
        db,
        action="calculation.create",
        user_id=actor.id,
        object_type="calculation",
        object_id=row.id,
        new_value=_snapshot(row),
    )
    return row


async def get_calculation(db: AsyncSession, calculation_id: uuid.UUID) -> Calculation:
    row = await db.get(Calculation, calculation_id)
    if row is None:
        raise err("ERR-SYS-003")
    return row


# --- Task 8: the public surface for levels 4+ (applications 3.9, payments ---
# 3.10, permits 3.11) ---------------------------------------------------------
#
# Eight entry points, and nothing else: `preview` and `save_calculation`
# above (Task 7), `LOAD_PROVIDERS` and `committed_load_sb` above (Task 4,
# ruling 12 — the seam and the reader over it are two separate things a
# caller touches, not one), `effective_norm`/`run_checks`/`latest_calculation`
# right below, and `calculator.from_input_snapshot` (I9) — VERIFICATION only,
# rebuilding the request/snapshot pair to confirm a stored row still
# recomputes to the same numbers, never for pricing a new one. A level-4+
# caller must NEVER:
#   - import `norms.repo` (or any other private module here) directly — every
#     fact it could read that way is already reachable through one of the
#     eight, the same reason a level-3 module reaches `gis` only through
#     `gis.service` (module boundary, CLAUDE.md);
#   - read `tariffs`/`rule_parameters`/`norms` as tables of its own — a rate
#     or a limit is only ever correct as of the SNAPSHOT `preview`/
#     `save_calculation`/`run_checks` resolved it under, never as a fresh
#     query against "whatever is published today";
#   - recompute a stored `Calculation`'s amount from its own
#     `input_snapshot` — a saved row is already the answer (`rule_code_version`
#     names the exact arithmetic that produced it), and a second, local
#     re-implementation is exactly the two-sources-of-truth risk this stage's
#     own `_compute`/`_build_request_and_snapshot` split exists to prevent.
#
# `effective_norm` is a thin pass-through: `repo.effective_norm` has existed
# since Task 3, but living only in `repo` would leave 3.11 with no LEGAL way
# to reach it at all (mirrors `gis.service.published_version`, added for the
# identical reason). `run_checks` is not a bare pass-through — it shares
# `_build_request_and_snapshot` with `_compute` but always calls
# `checks.run_checks` with `used_sb=None`, `checks.py`'s own "checks without
# the money" path (`_limit_check`'s docstring) — so a reviewer can see whether
# a request is admissible at all without ever pricing it, and a missing
# coefficient can never turn an admissibility screen into an error the way
# pricing legitimately would. `latest_calculation` (Task 8, applications plan
# 03.9a ruling C6) is the newest `calculations` row for an application — what
# 3.10 builds its invoice from, and `applications.service.current_calculation`
# delegates here rather than querying `calculations` itself. Like
# `effective_norm`, it is NOT a permission-checked read: the caller is
# another SERVICE inside this process, not an HTTP actor.


async def effective_norm(
    db: AsyncSession, contour_id: uuid.UUID, activity_type_id: uuid.UUID, on_date: date
) -> Norm | None:
    """The published norm in force for this contour × activity on `on_date`,
    or `None`. No permission or zone rule: the caller is another SERVICE
    inside this process, not an HTTP actor — mirrors
    `gis.service.published_version`."""
    return await repo.effective_norm(db, contour_id, activity_type_id, on_date)


async def run_checks(db: AsyncSession, *, payload: CalculationIn) -> list[checks.CheckResult]:
    """Every admissibility rule for `payload`, without the arithmetic — for a
    reviewer's screen (3.9) that needs to know whether a request is admissible
    before, or without ever, pricing it. Always resolves `used_sb=None`
    (never a manufactured zero load), so the limit check reports `skipped`
    rather than a computed comparison; every other check (norm, season,
    rotation, fire-ban, restrictions) runs exactly as it would inside
    `preview`/`save_calculation`, off the identical request/snapshot pair.

    Raises before any check runs — this is not a purely reporting call — when
    `period_to` precedes `period_from` or the period exceeds
    `checks.MAX_PERIOD_DAYS`: `checks.run_checks` guards both fail-closed for
    every caller, since a reversed period would otherwise no-op the season
    walk and invert `features_intersecting`'s validity predicate into a false
    `pass` on the fire ban."""
    request, snapshot = await _build_request_and_snapshot(db, payload)
    return await checks.run_checks(
        db,
        request=request,
        contour_id=payload.contour_id,
        activity_type_id=payload.activity_type_id,
        snapshot=snapshot,
        used_sb=None,
    )


async def latest_calculation(db: AsyncSession, application_id: uuid.UUID) -> Calculation | None:
    """The newest `calculations` row for this application, or `None` — what
    3.10 builds its invoice from (`applications.service.current_calculation`
    delegates here, ruling C6: `applications` may not query `calculations`
    itself, nor import `norms.repo`). Not a permission-checked read: the
    caller is another SERVICE inside this process, same as `effective_norm`.
    Reuses `repo.list_calculations` (newest-first, `id` tie-break since
    uuid7 is time-ordered) rather than a second query beside it."""
    rows, _total = await repo.list_calculations(
        db, application_id=application_id, limit=1, offset=0
    )
    return rows[0] if rows else None
