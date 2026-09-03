"""Applications service — business logic over the `applications` tables
(design/02 § applications; plan `03.9a-applications-core`).

Branch 1 (`stage-3.9a-core`) ships exactly three functions of plan Task 8's
public surface — `get`, `current_calculation`, `set_status` — on top of
`core/numbers.py` (moved forward out of Task 5) and the event bus shipped in
the two commits before this one. Branch 2 adds the rest: `precheck`,
`submit`, the duplicate guard, and the full decision flow (`start_review`,
`approve`, `reject`, `return_to_applicant`, `cancel`, `forward`). See the
"Task 8 public surface" comment below for the contract this file promises
levels 4+ (payments 3.10, permits 3.11) today."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.abac import Zone, zone_filter, zone_of
from app.core.errors import err
from app.core.schemas import PageParams
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import Organization
from app.modules.applications import repo
from app.modules.applications.models import (
    Application,
    ApplicationItem,
    ApplicationStatusHistory,
)
from app.modules.applications.permissions import (
    APPLICATIONS_DECIDE,
    APPLICATIONS_REVIEW,
    APPLICATIONS_VIEW_ANY,
)
from app.modules.applications.schemas import ApplicationCreate, ApplicationPatch
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth import service as auth_service
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.norms import service as norms_service
from app.modules.norms.models import Calculation

# Ruling 17 (decision #38): audit action codes are "<object>.<verb>" in
# English, and the constant lives with the acting module — audit (level 0)
# knows no domain vocabulary. `set_status` is the ONLY writer in this branch,
# so it is the only constant declared so far; branch 2 adds
# APPLICATION_SUBMIT/.start_review/.approve/.reject/.cancel/.forward
# alongside it, because a flow verb audits under its own name whether or not
# it writes its transition through `set_status` — and `submit` cannot write it
# through `set_status` at all (ruling 25; see `set_status`'s own docstring).
APPLICATION_STATUS_CHANGE = "application.status_change"
# Task 3's own two flow verbs, plus the one a REFUSED read writes. A verb that
# does more than move a status audits under its own name (the same split
# `permits.service` makes) — and `create`/`update` move no status at all.
APPLICATION_CREATE = "application.create"
APPLICATION_UPDATE = "application.update"
# Written ONLY on a territorial denial, never on a successful read (the shape
# `permits.service.PERMIT_READ` established): a GET that audits every hit lets
# anyone holding a session write an `audit_log` row per request, while a GET
# that audits nothing leaves `tz/10`'s RI-12 — «попытка доступа вне
# территориальных полномочий», High and immediate — with no way to fire on a
# read at all. See `_readable_application`.
APPLICATION_READ = "application.read"

# `applications.channel` is NOT NULL and 3.9a has exactly one channel: the
# portal. `mygov` arrives with the my.gov.uz integration and will be set by
# whatever adapter files on a citizen's behalf, never guessed from a request
# body — a client that could name its own channel could claim a filing came
# through a state portal it never touched.
CHANNEL_PORTAL = "portal"
# `applications.kind`: an `extension` is a child of an existing application
# (`parent_application_id`) and is 3.11b's `POST /permits/{id}/extend`. Every
# application this module creates is `new`.
KIND_NEW = "new"
INITIAL_STATUS = "DRAFT"
# The classifier a `benefit_category_item_id` must belong to — seeded by
# migration 0005 and the same catalogue `norms.calculator` resolves a
# `benefit_code` against. Checking membership, not merely that the id names
# some classifier item, is what stops a rejection reason being posted as a
# benefit.
BENEFIT_CLASSIFIER_CODE = "benefit_categories"

# tz/05's transition table, verbatim (plan 03.9a task 8, the brief's own
# copy). All fourteen `APPLICATION_STATUSES` are keys; `ARCHIVED` is
# terminal. No status maps to itself — tz/05 has no self-loop anywhere in
# this table, so a transition to the status an application already holds is
# exactly as illegal as any other jump not listed here (3.10's retry paths
# will attempt this; see `test_public_surface.py`).
APPLICATION_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"SUBMITTED", "CANCELLED"}),
    "SUBMITTED": frozenset({"IN_REVIEW", "RETURNED", "REJECTED"}),
    "IN_REVIEW": frozenset({"PENDING_INFO", "APPROVED", "REJECTED", "RETURNED"}),
    "PENDING_INFO": frozenset({"IN_REVIEW", "CANCELLED"}),
    "RETURNED": frozenset({"SUBMITTED", "CANCELLED"}),
    "APPROVED": frozenset({"INVOICED"}),
    "INVOICED": frozenset({"PAID", "EXPIRED_UNPAID", "CANCELLED"}),
    "PAID": frozenset({"PERMIT_ISSUED"}),
    "PERMIT_ISSUED": frozenset({"CLOSED"}),
    "REJECTED": frozenset({"ARCHIVED"}),
    "CANCELLED": frozenset({"ARCHIVED"}),
    "EXPIRED_UNPAID": frozenset({"ARCHIVED"}),
    "CLOSED": frozenset({"ARCHIVED"}),
    "ARCHIVED": frozenset(),
}
# One-source-of-truth check: the keys and every target above must be members
# of APPLICATION_STATUSES (models.py) — see
# test_public_surface.py::test_transition_table_has_all_fourteen_statuses_and_only_real_targets.


def _assert_transition(application: Application, to_status: str) -> None:
    """A transition not listed in `APPLICATION_TRANSITIONS[application.status]`
    is a conflict with the application's CURRENT state, not a malformed
    request body — `ERR-APP-004` (409), never `ERR-VAL-001`. Mirrors
    `gis.service._assert_transition`'s identical shape over `TRANSITIONS`."""
    if to_status not in APPLICATION_TRANSITIONS[application.status]:
        raise err(
            "ERR-APP-004",
            details={"reason": "bad_transition", "from": application.status, "to": to_status},
        )


# --- Task 8: the public surface for levels 4+ (payments 3.10, permits 3.11) -
#
# Branch 1 ships three of Task 8's functions below — `get`,
# `current_calculation`, `set_status` — plus the four event names already
# shipped in `applications/events.py`. Branch 2 adds `precheck`, `submit` and
# the decision flow; everything below still applies to the FULL surface,
# stated now because 3.10a and 3.11 are blocked on it today, not when branch
# 2 lands.
#
# - `get(db, application_id) -> Application | None` — no permission or zone
#   rule; the caller is another service inside this process, mirroring
#   `gis.service.published_version` and `norms.service.effective_norm`.
# - `current_calculation(db, application_id) -> Calculation | None` — the
#   newest calculation for the application. 3.10 builds its invoice from
#   exactly this.
# - `set_status(db, application_id, *, to_status, actor=None, reason=None) ->
#   Application` — the ONE way a level-4 module moves an application.
#   It validates the transition against tz/05, writes the history row and
#   the audit entry, and refuses an illegal jump. Like `get` above, it
#   enforces NO permission or zone rule of its own — the calling module's
#   router must (review I1: this is not an oversight, it is the same "the
#   caller is another service" design as every function on this page, and a
#   check here could not span, say, `INVOICED`/`PERMIT_ISSUED` across two
#   different modules' permission codes). A repeat call for a status the
#   application already holds is a transition to itself, which
#   `APPLICATION_TRANSITIONS` never contains — it raises `ERR-APP-004` with
#   `details["from"] == details["to"]`, which is how a retrying caller (3.10's
#   own retry paths) tells "already applied, harmless" from a genuinely
#   illegal jump.
#
#   WHICH TARGETS A LEVEL-4 MODULE MAY DRIVE — a limit, not an example
#   (final review I1). `APPLICATION_TRANSITIONS` is the FULL tz/05 table
#   because `_assert_transition` validates against all of it; it is not a
#   menu. The only targets a module above this one may pass as `to_status`:
#
#       3.10 payments — INVOICED, PAID, EXPIRED_UNPAID
#       3.11 permits  — PERMIT_ISSUED, CLOSED
#       4.5 archive   — ARCHIVED (nobody's in stage 3)
#
#   SUBMITTED, IN_REVIEW, APPROVED, REJECTED, RETURNED, PENDING_INFO and
#   CANCELLED belong to the applicant/staff flow and are branch 2's own flow
#   verbs (`submit`, `start_review`, `approve`, `reject`,
#   `return_to_applicant`, `cancel`, `forward`). Do NOT reach them through
#   `set_status`, from any module including this one.
#
#   Why, concretely: `set_status` moves `status` and nothing else. It does
#   not know whether the application is COMPLETE, and design/02's "a null
#   `contour_id` is allowed only in DRAFT" is enforced nowhere in the schema
#   — the EXCLUDE constraint `ex_applications_no_duplicate` cannot see such a
#   row either, because its WHERE requires `contour_id`/`period_from`/
#   `period_to` to be non-null. So
#   `set_status(db, half_empty_draft, to_status="SUBMITTED")` yields a
#   SUBMITTED application with no contour, no period and no activity type,
#   invisible to the one-active-application-per-plot guard and in a state
#   `tz/05` invariant 1 says cannot exist. The completeness check that makes
#   SUBMITTED safe lives in branch 2's `submit`, together with the number
#   allocation, the check rows and the signature — none of which this
#   function performs.
#
#   Deliberately a rule and not a runtime allow-list. Branch 2's flow verbs
#   may legitimately choose to write their transition through `set_status`
#   (only `submit` is forced around it, by ruling 25's explicit history-row
#   id), and 3.9b's RETURNED -> SUBMITTED resubmission has the same freedom;
#   a whitelist frozen in branch 1 would be unpicked in branch 2, which is
#   worse than a stated limit. It would also not fix the invariant it looks
#   like it fixes — a half-empty DRAFT reaching CANCELLED, or a half-empty
#   application reaching INVOICED, is the same hole, so the fix is
#   completeness at the flow verb, never a narrower target list here.
# - The four event names in `applications.events`: APPLICATION_SUBMITTED,
#   APPLICATION_APPROVED, APPLICATION_REJECTED, APPLICATION_CANCELLED.
#
# The signature identities (ruling 25), because 3.11 collects signatures and
# must not guess: a SUBMISSION is signed as `("application_submission", <the
# SUBMITTED history row's id>, "application_submit")`; a DECISION is signed
# as `("application", <application id>, "application_decision")`. An
# application with three submission attempts has three separate signature
# objects and one decision object.
#
# A level-4 caller must NEVER import `applications.repo` or
# `applications.models`, and must never UPDATE `applications.status`
# directly — `set_status` exists so that every transition in the system has
# one implementation, one history row and one audit entry.
#
# No consumer reads a check as singular. The card returns `checks` as the
# full list and the reviewer's screen shows the history (ruling 12). Do NOT
# add a `latest_check` helper or a `DISTINCT ON (check_type)` read: 3.10
# reading "the newest calculation" is correct because a calculation is a
# price and only the last one is owed; a check is evidence, and evidence is
# a list.


async def get(db: AsyncSession, application_id: uuid.UUID) -> Application | None:
    """The application row, or `None`. No permission or zone rule: the
    caller is another SERVICE inside this process, mirroring
    `gis.service.published_version` and `norms.service.effective_norm`."""
    return await repo.get_application(db, application_id)


async def current_calculation(db: AsyncSession, application_id: uuid.UUID) -> Calculation | None:
    """The newest calculation for the application — what 3.10 builds its
    invoice from. Delegates to `norms.service.latest_calculation` (ruling
    C6): `applications` is level 3 and may call `norms` (level 2) only
    through its service, never by querying `calculations` itself or
    importing `norms.repo` (module boundary, CLAUDE.md)."""
    return await norms_service.latest_calculation(db, application_id)


async def set_status(
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    to_status: str,
    actor: User | None = None,
    reason: str | None = None,
) -> Application:
    """The ONE way a level-4 module moves an application (public surface
    above): validates the transition against tz/05, writes the history row
    and the audit entry, and refuses an illegal jump.

    Touches ONLY `applications.status` — `decided_at` and any other flow
    timestamp belong to the flow action that owns it (branch 2), never to
    this function.

    Frozen signature (controller ruling C5): do not add parameters, do not
    make `to_status` positional, do not add an `action`/`id` override.
    Branch 2's `submit` needs to supply the SUBMITTED history row's id
    explicitly (ruling 25, the signature-identity note above), which this
    signature cannot express — `submit` writes its own transition instead of
    routing through here, and stays audited under its own flow-verb constant
    (ruling 17: `APPLICATION_SUBMIT` and siblings), never
    `APPLICATION_STATUS_CHANGE`.

    Locks the row for the duration of the call (review C1): this is the
    single write path every level-4 module uses for every transition, and
    two of its callers are already named in the plan — a scheduler job
    (INVOICED -> EXPIRED_UNPAID) and an HTTP payment callback (INVOICED ->
    PAID) — that can genuinely race on the same application. Without the
    lock both read the same pre-write status, both pass `_assert_transition`,
    and the second UPDATE silently overwrites the first with no error and an
    `application_status_history` that claims two transitions FROM a status
    the application was only in once. The second caller here instead blocks
    until the first commits or rolls back, then re-reads the now-current
    status, so a genuine conflict surfaces as `ERR-APP-004` — a clean 409 —
    rather than a lost write.
    """
    # get_application_for_update, never plain `get`/`repo.get_application`:
    # see the lock note above.
    application = await repo.get_application_for_update(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})

    _assert_transition(application, to_status)
    from_status = application.status
    application.status = to_status

    history = ApplicationStatusHistory(
        application_id=application.id,
        from_status=from_status,
        to_status=to_status,
        changed_by=actor.id if actor else None,
        reason_text=reason,
    )
    await repo.add_status_history(db, history)
    # Same `onupdate=func.now()` expiry a plain UPDATE leaves behind (lesson:
    # "the row in memory is not what Postgres stored") — refresh before a
    # caller reads `updated_at` off the returned row.
    await db.refresh(application)

    await audit.log(
        db,
        action=APPLICATION_STATUS_CHANGE,
        user_id=actor.id if actor else None,
        object_type="application",
        object_id=application.id,
        old_value={"status": from_status},
        new_value={"status": to_status},
        basis=reason,
    )
    return application


# --- Task 3: the draft, and who may read an application ----------------------


def _json_safe(value: Any) -> Any:
    """Audit snapshots go through `audit_log`'s JSONB columns via the stock
    `json.dumps` (nothing in this app configures an encoder — lesson), and an
    application's own columns are exactly `UUID`, `date` and `Decimal`. Mirrors
    `gis.service._json_safe` and `admin.users_service._json_safe`: kept local,
    like both of those, because a private helper of another module is not part
    of its public surface."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _snapshot(application: Application, items: list[ApplicationItem]) -> dict[str, Any]:
    """What an audit entry records about a draft: everything a PATCH can move,
    and nothing else. `status` is absent on purpose — `set_status` is the only
    thing that moves it and it audits that move itself, under its own action.

    **`items` is part of the snapshot, not an afterthought** (review I1). The
    herd is the field on this table that matters most: it drives the fee, the
    norm's SB limit and the printed permit. A snapshot of the six scalar
    columns alone answers a `prosecutor` reading `audit_log` that a PATCH
    taking 40 head to 4000 changed NOTHING — an entry asserting that is worse
    than no entry at all.

    Sorted by `livestock_type_id`, because `repo.replace_items` re-inserts the
    whole list and `list_items`' own order is insertion order: unsorted, the
    same herd re-sent in a different order would read as a change, and this
    trail's one job is that a difference means a difference.
    """
    snapshot: dict[str, Any] = {
        name: _json_safe(getattr(application, name))
        for name in (
            "activity_type_id",
            "contour_id",
            "period_from",
            "period_to",
            "quantity",
            "benefit_category_item_id",
        )
    }
    snapshot["items"] = [
        {"livestock_type_id": str(item.livestock_type_id), "head_count": item.head_count}
        for item in sorted(items, key=lambda row: str(row.livestock_type_id))
    ]
    return snapshot


async def _holds_staff_read(db: AsyncSession, actor: User) -> bool:
    """Whether this actor may see applications that are not their own at all —
    the "may this role, ever" half of a read rule whose other half is the zone
    (lesson: zone scoping is not a permission check; a read path needs both).

    THREE codes, not just `applications.view_any`. Migration 0015 grants
    `view_any` to `prosecutor` alone, while the hodim who takes an application
    into work holds `applications.review` (`executor_staff`) and the head who
    decides it holds `applications.decide` (`executor_head`) — a rule reading
    `view_any` alone would lock both of them out of the very applications they
    exist to process, and no migration says they may not look.

    Plus `sys_admin`, which passes every permission gate (decision #41 ruling
    2) and therefore must pass this one too, exactly as
    `permits.service._holds_view_any` and `signatures.service._holds_view_any`
    do — a rule checked INSIDE a handler does not get `require_permission`'s
    superuser branch for free.

    It cannot be a route-level dependency: these routes also admit the
    application's own APPLICANT, who holds none of the three, so the dependency
    would reject a citizen reading their own draft before the ownership check
    ever ran.
    """
    if await auth_service.role_code(db, actor) == SUPERUSER_ROLE:
        return True
    held = await auth_repo.permission_codes(db, actor)
    return not held.isdisjoint({APPLICATIONS_VIEW_ANY, APPLICATIONS_REVIEW, APPLICATIONS_DECIDE})


def _organization_in_zone(zone: Zone, org: Organization) -> bool:
    """Per-row equivalent of `zone_filter`'s SQL for ONE organization row — a
    LOCAL copy of the private helper of the same name and identical logic in
    `gis.service`, `norms.service` and `permits.service`. The module boundary
    rules out importing any of them: a private helper is not part of a module's
    declared public surface."""
    if zone.region_id is not None and zone.region_id != org.region_id:
        return False
    if zone.district_id is not None and zone.district_id != org.district_id:
        return False
    if zone.organization_id is not None and zone.organization_id != org.id:
        return False
    return True


async def _organization_in_actor_zone(
    db: AsyncSession, actor: User, organization_id: uuid.UUID
) -> bool:
    """Whether this actor's zone covers this organization.

    ALL THREE axes of `app/core/abac.py`'s `Zone`, never `organization_id`
    alone: an actor with a region — or a district — but no organization of
    their own would otherwise pass for every organization in the country, and
    that shape is creatable today (`admin.users_service.create_user` sets the
    three columns independently). This is the same correction 3.6a's final
    review made to `gis.service._assert_in_zone`.

    A zone empty on every axis is republic-wide and short-circuits before the
    read, so the common case costs no query. Reference data is read through
    `admin.repo` (CLAUDE.md); `db.get`'s identity map makes a repeated lookup
    inside one request free.
    """
    zone = zone_of(actor)
    if zone == Zone(None, None, None):
        return True
    org = await admin_repo.get_organization(db, organization_id)
    return org is not None and _organization_in_zone(zone, org)


async def _effective_organization(db: AsyncSession, application: Application) -> uuid.UUID | None:
    """Which leshoz an application belongs to for the purposes of a zone rule:
    `assigned_org_id` once a reviewer has taken it into work, the contour's
    owner before that, and `None` while the draft still names no contour.

    `assigned_org_id` is null for every DRAFT and stays null through SUBMITTED
    until `start-review` writes the assignment (plan ruling 14) — so a rule
    reading that column alone would hide from a hodim exactly the applications
    they are supposed to pick up. The contour's owner is resolved through
    `gis.service`, never `gis.repo` and never a query of `contours` here
    (module boundary), the same way `norms.service._assert_norm_zone` and
    `permits.service._assert_in_zone` resolve theirs.

    `repo._zone_join_target` is this same rule as SQL, for the paged list that
    cannot resolve one row at a time. The two must move together.
    """
    if application.assigned_org_id is not None:
        return application.assigned_org_id
    if application.contour_id is None:
        return None
    return await gis_service.contour_organization(db, application.contour_id)


async def _own_applicant_ids(db: AsyncSession, actor: User) -> list[uuid.UUID]:
    """Every `applicants` row this user may act for today — their own
    individual row plus every legal entity they hold an EFFECTIVE
    representation of. `auth.service` owns that definition and judges
    "effective" against `business_today()`, so a lapsed power of attorney stops
    working the day it lapses.

    One definition, three uses: the card asks about one application, the list
    needs the SET to build a query out of, and `patch_draft` asks the same
    question a third time. A separate per-row predicate beside a separate list
    scope is two rules that agree until one of them is edited (the reasoning
    `permits.service._is_holder` spells out in full)."""
    return await auth_service.own_applicant_ids(db, actor.id)


async def _readable_application(
    db: AsyncSession, application_id: uuid.UUID, *, actor: User
) -> Application:
    """The application `actor` is allowed to read, or a refusal.

    **Every refusal here is 404 `ERR-SYS-003`** — the same answer an id that
    never existed gets. An application is not a published document: it carries a
    citizen's name, plot and herd from the moment it is created, and a 403 would
    make this route an application-existence oracle for anybody holding a
    session. This is where the rule differs from `permits.service.
    _readable_permit`, which answers 403 `ERR-ACL-002` to a `view_any` holder
    outside the zone: a permit's existence is already public through the QR
    check, and an application's is not.

    **The territorial refusal still writes an RI-12 trail before it raises**
    (`tz/10`: «попытка доступа вне территориальных полномочий», High,
    immediate), through the early-commit-on-denial pattern (decision #40 ruling
    2) — the raise would otherwise roll the trail back together with the very
    exception it exists to explain. Only that branch is audited: a caller
    holding none of the three staff codes cannot be "outside their zone", they
    have no zone claim to exceed, and a row per 404 would let any signed-in
    citizen fill `audit_log` by guessing uuids.

    An application whose organization cannot be resolved at all — a draft with
    no contour yet — is outside every ZONED actor's zone, and inside a
    republic-wide one's, which is what the ordering below says: the zone-free
    short-circuit comes first.
    """
    application = await repo.get_application(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    if application.applicant_id in await _own_applicant_ids(db, actor):
        return application
    if not await _holds_staff_read(db, actor):
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    if zone_of(actor) == Zone(None, None, None):
        return application
    organization_id = await _effective_organization(db, application)
    if organization_id is not None and await _organization_in_actor_zone(
        db, actor, organization_id
    ):
        return application
    await audit.log(
        db,
        action=APPLICATION_READ,
        user_id=actor.id,
        object_type="application",
        object_id=application.id,
        result="denied",
        basis="out_of_zone",
        extra={"risk_indicator": "RI-12"},
    )
    await db.commit()
    raise err("ERR-SYS-003", details={"application": str(application_id)})


async def _resolve_applicant(
    db: AsyncSession, payload: ApplicationCreate, *, actor: User
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Whose application this is, and on whose authority — `(applicant_id,
    representation_id)`.

    `on_behalf="self"`: the caller's own `applicants` row and no other. A
    supplied `applicant_id` naming somebody else is refused rather than
    ignored, because "ignored" is how a client ends up believing it filed for
    the person it named.

    `on_behalf="legal"`: a legal entity has no account of its own (decision
    #9), so the caller must hold an EFFECTIVE representation of it — and the
    representation's own id is stored, because `applications.representation_id`
    is the record of which power of attorney was acted under. A representation
    that has lapsed or been revoked is not effective, judged against
    `business_today()` inside `auth.service`.
    """
    if payload.on_behalf == "self":
        own = await auth_service.get_own_applicant(db, actor.id)
        if own is None:
            raise err("ERR-VAL-001", details={"reason": "no_own_applicant"})
        if payload.applicant_id is not None and payload.applicant_id != own.id:
            raise err("ERR-VAL-001", details={"reason": "applicant_is_not_the_caller"})
        return own.id, None
    if payload.applicant_id is None:
        raise err("ERR-VAL-001", details={"reason": "applicant_id_required"})
    representation = await auth_service.effective_representation_of(
        db, user_id=actor.id, applicant_id=payload.applicant_id
    )
    if representation is None:
        raise err("ERR-ACL-001", details={"reason": "no_effective_representation"})
    return payload.applicant_id, representation.id


async def create_draft(db: AsyncSession, payload: ApplicationCreate, *, actor: User) -> Application:
    """`POST /applications` — an EMPTY draft, and deliberately so (ruling 7):
    tz/04 С3 autosaves a draft field by field, so everything except who is
    filing and for whom arrives later through `PATCH`.

    The `DRAFT` row of `application_status_history` is written HERE, directly,
    and not through `set_status`: `APPLICATION_TRANSITIONS` has no edge INTO
    `DRAFT` — nothing may return an application to it — so `set_status` could
    not write this row even if asked. It is written all the same because a
    timeline that starts at `SUBMITTED` cannot say when the citizen began, and
    nothing else in the system will ever be in a position to add it.
    """
    applicant_id, representation_id = await _resolve_applicant(db, payload, actor=actor)
    application = Application(
        applicant_id=applicant_id,
        submitted_by_user_id=actor.id,
        on_behalf=payload.on_behalf,
        representation_id=representation_id,
        status=INITIAL_STATUS,
        channel=CHANNEL_PORTAL,
        kind=KIND_NEW,
    )
    db.add(application)
    await db.flush()
    await repo.add_status_history(
        db,
        ApplicationStatusHistory(
            application_id=application.id,
            from_status=None,
            to_status=INITIAL_STATUS,
            changed_by=actor.id,
        ),
    )
    # `created_at`/`updated_at` are `server_default=func.now()`, so the row in
    # memory is not what Postgres stored until it is read back (lesson) — and
    # this row is serialized into the 201 response.
    await db.refresh(application)
    await audit.log(
        db,
        action=APPLICATION_CREATE,
        user_id=actor.id,
        object_type="application",
        object_id=application.id,
        new_value={
            "applicant_id": str(applicant_id),
            "on_behalf": payload.on_behalf,
            "representation_id": None if representation_id is None else str(representation_id),
            "status": INITIAL_STATUS,
        },
    )
    return application


async def _assert_references(db: AsyncSession, fields: dict[str, Any]) -> None:
    """Every caller-settable FK in a PATCH body, checked before `flush()`.

    Not optional politeness: an unknown id reaching the INSERT/UPDATE is an
    `IntegrityError`, which has no handler in `app/main.py` and surfaces as
    `ERR-SYS-001`/500 for what is only ever a typo (lesson: walk every
    caller-settable field that is an FK and confirm each has a service guard
    ahead of `flush()`).

    These are EXISTENCE checks, not validity checks (lesson): that an activity
    type is choosable today, that a contour is a real contour, that a benefit
    category is one of the benefit classifier's items. Whether the applicant is
    ENTITLED to that benefit is the submission's question (ruling 10а: a claim
    needs a supporting document), and whether the contour may be grazed at all
    is the pre-check's.

    Reference data is read through `admin.repo` and the contour through
    `gis.service` — never a re-query of either module's tables (CLAUDE.md).
    """
    activity_type_id = fields.get("activity_type_id")
    if activity_type_id is not None:
        activity = await admin_repo.get_activity_type(db, activity_type_id)
        if activity is None or activity.status != "active":
            raise err("ERR-VAL-001", details={"reason": "unknown_activity_type"})

    contour_id = fields.get("contour_id")
    if contour_id is not None and await gis_service.contour_organization(db, contour_id) is None:
        raise err("ERR-VAL-001", details={"reason": "unknown_contour"})

    benefit_item_id = fields.get("benefit_category_item_id")
    if benefit_item_id is not None:
        benefit_item = await admin_repo.get_classifier_item(db, benefit_item_id)
        classifier = await admin_repo.get_classifier_by_code(db, BENEFIT_CLASSIFIER_CODE)
        if (
            benefit_item is None
            or classifier is None
            or benefit_item.classifier_id != classifier.id
        ):
            raise err("ERR-VAL-001", details={"reason": "unknown_benefit_category"})

    items = fields.get("items")
    if items:
        wanted = [item["livestock_type_id"] for item in items]
        if len(set(wanted)) != len(wanted):
            # `uq_application_items_livestock` would refuse this at flush as an
            # IntegrityError/500; a repeated species is a client bug with an
            # obvious name, so it gets one.
            raise err("ERR-VAL-001", details={"reason": "duplicate_livestock_type"})
        known = {row.id for row in await admin_repo.list_livestock_types(db)}
        if not set(wanted).issubset(known):
            raise err("ERR-VAL-001", details={"reason": "unknown_livestock_type"})


async def _own_draft_for_update(
    db: AsyncSession, application_id: uuid.UUID, *, actor: User
) -> Application:
    """The caller's own application, locked, and only while it is still a
    DRAFT.

    Ownership is checked BEFORE the status, and both refusals differ: a
    stranger gets 404 (they may not learn that this id is an application at
    all), while the owner of an application that has moved on gets 409
    `ERR-APP-004` — a conflict with the application's current state, which is
    the honest answer to "why can I no longer edit this". In 3.9a `DRAFT` is
    the only editable status; 3.9b adds `RETURNED`, when a returned application
    becomes correctable again.

    Locked (`repo.get_application_for_update`) because this is a read-check-
    write over `status`: without it a PATCH and a concurrent `submit` both read
    `DRAFT`, both pass, and the edit lands on an application that is already
    submitted — its signed package then describes something the stored row no
    longer says.
    """
    application = await repo.get_application_for_update(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    if application.applicant_id not in await _own_applicant_ids(db, actor):
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    if application.status != INITIAL_STATUS:
        raise err("ERR-APP-004", details={"reason": "not_draft", "status": application.status})
    return application


async def patch_draft(
    db: AsyncSession, application_id: uuid.UUID, patch: ApplicationPatch, *, actor: User
) -> Application:
    """`PATCH /applications/{id}` — any subset of the draft's own fields.

    `exclude_unset=True` is what makes "any subset" true: a field the body did
    not mention keeps its value, while one sent as `null` is CLEARED. That
    distinction is the whole point of an autosaved draft — an applicant who
    picked the wrong contour must be able to unpick it.

    `items` is replaced WHOLESALE, never merged: an applicant removing a
    livestock kind must be able to, and a merge could only ever add.

    No completeness check and no period-ordering check: ruling 7 puts both in
    the pre-check (task 4) and the submission (task 5). `requested_area_ha` is
    not here at all — it is frozen at submission from the contour version's own
    `area_ha` (ruling 22), and `ApplicationPatch` forbids unknown fields so a
    client that tries to set it is told 422 rather than silently ignored.
    """
    application = await _own_draft_for_update(db, application_id, actor=actor)
    fields = patch.model_dump(exclude_unset=True)
    await _assert_references(db, fields)
    # Read BEFORE the replacement: `repo.replace_items` deletes the old rows,
    # so afterwards there is nothing left to snapshot them from.
    before = _snapshot(application, await repo.list_items(db, application.id))
    items = fields.pop("items", None)
    for name, value in fields.items():
        setattr(application, name, value)
    if items is not None:
        await repo.replace_items(
            db,
            application.id,
            [
                ApplicationItem(
                    application_id=application.id,
                    livestock_type_id=item["livestock_type_id"],
                    head_count=item["head_count"],
                )
                for item in items
            ],
        )
    await db.flush()
    # `updated_at` is `onupdate=func.now()`, which SQLAlchemy leaves EXPIRED
    # after a plain UPDATE, and a caller-supplied `quantity` round-trips at
    # NUMERIC(12,4)'s own scale rather than the caller's (lesson: the row in
    # memory is not what Postgres stored) — both are in this response.
    await db.refresh(application)
    await audit.log(
        db,
        action=APPLICATION_UPDATE,
        user_id=actor.id,
        object_type="application",
        object_id=application.id,
        old_value=before,
        new_value=_snapshot(application, await repo.list_items(db, application.id)),
    )
    return application


async def get_card(db: AsyncSession, application_id: uuid.UUID, *, actor: User) -> dict[str, Any]:
    """`GET /applications/{id}` — the application, its items, its documents, its
    checks and its current price.

    `checks` is the FULL list, never the latest per type: a repeat check is a
    new row and the history is the evidence (ruling 12). `calculation` is
    `current_calculation` — the newest `calculations` row, which is what
    `payments` invoices from — read through this module's own public surface
    rather than by querying `norms`' tables, so the card and the invoice can
    never disagree about which calculation is current.

    Both keys are present and empty/null from task 3, before anything can write
    either: they are a contract 3.10a and 3.11a already read
    (`card["calculation"]["amount"]`), not a placeholder a later task adds.
    """
    application = await _readable_application(db, application_id, actor=actor)
    return {
        "application": application,
        "items": await repo.list_items(db, application.id),
        "documents": await repo.list_documents(db, application.id),
        "checks": await repo.list_checks(db, application.id),
        "calculation": await current_calculation(db, application.id),
    }


async def list_applications(
    db: AsyncSession,
    *,
    actor: User,
    params: PageParams,
    status: str | None = None,
    activity_type_id: uuid.UUID | None = None,
    contour_id: uuid.UUID | None = None,
    applicant_id: uuid.UUID | None = None,
    number: str | None = None,
    period_from: date | None = None,
    period_to: date | None = None,
) -> tuple[list[Application], int]:
    """`GET /applications` — one page of the applications `actor` may see, plus
    the total.

    The scope is the UNION of the two things `_readable_application` admits one
    at a time, so the list can never disagree with the card: the caller's own
    applications, OR — for a staff member holding one of the three read codes —
    everything inside their zone. A republic-wide staff member's `zone_filter`
    is `true()` and they see the lot; a zone-scoped one sees their own leshoz,
    and an application outside it is simply ABSENT rather than refused, because
    a filter has no way to answer 403 and nobody named a target.

    A caller who is neither gets `([], 0)` and no statement is issued: an empty
    scope must mean "nothing", and building it as a WHERE clause would leave one
    editing mistake between here and "every application in the country".

    All three zone axes are supplied. `applications` carries an organization id
    and no region or district, so `repo.list_applications` joins
    `organizations` — `zone_filter` FAILS CLOSED and raises when an axis is set
    without its column, and a region-scoped, organization-less actor is
    creatable today (`admin.users_service.create_user` sets the three
    independently). `organization_col` is `Organization.id` and not
    `Application.assigned_org_id`, because the row joined is the EFFECTIVE
    organization — assigned, or the contour's owner while the application is
    still unassigned (`repo._zone_join_target`, `_effective_organization`).

    The contour half of that join is `gis.service.contour_organization_column`,
    called HERE and handed to the repo as an expression: cross-module calls
    live in the service layer, and a repo calling another module's service
    inverts the layering even where the boundary rule itself is satisfied
    (review I2).
    """
    scope: list[Any] = []
    holder_ids = await _own_applicant_ids(db, actor)
    if holder_ids:
        scope.append(Application.applicant_id.in_(holder_ids))
    if await _holds_staff_read(db, actor):
        scope.append(
            zone_filter(
                zone_of(actor),
                region_col=Organization.region_id,
                district_col=Organization.district_id,
                organization_col=Organization.id,
            )
        )
    if not scope:
        return [], 0
    return await repo.list_applications(
        db,
        scope=or_(*scope),
        # Built here and passed down, exactly as `scope` above is (review I2).
        contour_organization_col=gis_service.contour_organization_column(Application.contour_id),
        status=status,
        activity_type_id=activity_type_id,
        contour_id=contour_id,
        applicant_id=applicant_id,
        number=number,
        period_from=period_from,
        period_to=period_to,
        offset=params.offset,
        limit=params.page_size,
    )
