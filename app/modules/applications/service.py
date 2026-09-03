"""Applications service — business logic over the `applications` tables
(design/02 § applications; plan `03.9a-applications-core`).

Branch 1 (`stage-3.9a-core`) ships exactly three functions of plan Task 8's
public surface — `get`, `current_calculation`, `set_status` — on top of
`core/numbers.py` (moved forward out of Task 5) and the event bus shipped in
the two commits before this one. Branch 2 adds the rest — task 3 the
draft's own four routes, task 4 `precheck` and the documents, and the tasks
after it `submit`, the duplicate guard and the full decision flow
(`start_review`, `approve`, `reject`, `return_to_applicant`, `cancel`,
`forward`). See the
"Task 8 public surface" comment below for the contract this file promises
levels 4+ (payments 3.10, permits 3.11) today."""

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.abac import Zone, zone_filter, zone_of
from app.core.errors import err
from app.core.events import Event, publish
from app.core.models import MediaFile
from app.core.numbers import next_public_number
from app.core.schemas import PageParams
from app.core.time import business_today
from app.db import uuid7
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import Organization
from app.modules.applications import checks, repo
from app.modules.applications.events import APPLICATION_CANCELLED, APPLICATION_SUBMITTED
from app.modules.applications.models import (
    Application,
    ApplicationAssignment,
    ApplicationDocument,
    ApplicationItem,
    ApplicationStatusHistory,
)
from app.modules.applications.permissions import (
    APPLICATIONS_DECIDE,
    APPLICATIONS_REVIEW,
    APPLICATIONS_VIEW_ANY,
)
from app.modules.applications.schemas import (
    ApplicationCreate,
    ApplicationDocumentIn,
    ApplicationPatch,
)
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth import service as auth_service
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.norms import service as norms_service
from app.modules.norms.models import Calculation
from app.modules.norms.schemas import CalculationIn
from app.modules.notifications import service as notifications_service
from app.modules.signatures import service as signatures_service

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
# Task 4's three. A pre-check writes `application_checks` rows, so it is a
# state-changing action and audits like one — once, here, never a row per check
# (`checks.run_all` deliberately audits nothing of its own: task 5's `submit`
# calls it too and audits under `application.submit`).
APPLICATION_PRECHECK = "application.precheck"
APPLICATION_DOCUMENT_ATTACH = "application_document.attach"
APPLICATION_DOCUMENT_DETACH = "application_document.detach"

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
# The classifier an `application_documents.doc_type_item_id` must belong to
# (seeded by migration 0005; its ITEMS are the Agency's to fill in). Checked for
# membership, not merely that the id names some classifier item — otherwise a
# rejection reason could be attached as a document type.
DOC_TYPE_CLASSIFIER_CODE = "doc_types"
# The ONE `doc_types` item a benefit claim is proven with (ruling 10а, made
# fail-closed by review round 2's important 5). Named here so a reader can see
# WHAT the submission looks for; migration 0005 seeds the classifier but none
# of its items — they are the Agency's to supply — so until this code exists
# and is active, `_assert_benefit_documents` refuses every benefit claim rather
# than accepting an unchecked attachment as proof.
BENEFIT_DOC_TYPE_CODE = "benefit_proof"

# tz/05's transition table (plan 03.9a task 8, the brief's own copy). All
# fourteen `APPLICATION_STATUSES` are keys; `ARCHIVED` is terminal. No status
# maps to itself — tz/05 has no self-loop anywhere in this table, so a
# transition to the status an application already holds is exactly as illegal
# as any other jump not listed here (3.10's retry paths will attempt this; see
# `test_public_surface.py`).
#
# **`SUBMITTED -> CANCELLED` and `IN_REVIEW -> CANCELLED` were added by task 6**
# (controller ruling R20). Branch 1 transcribed this table as tz/05 verbatim and
# dropped both: tz/05 lets an applicant WITHDRAW at any point before a decision,
# and without those two edges `POST /applications/{id}/cancel` could only ever
# work on a draft — an applicant who had already filed would have to wait for a
# decision on a permit they no longer want, and the plot would stay blocked by
# `ex_applications_no_duplicate` in the meantime. Purely additive, and it cannot
# widen what a level-4 module may do: CANCELLED is not in the target list the
# public-surface comment below permits, and `cancel` is this module's own flow
# verb.
APPLICATION_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"SUBMITTED", "CANCELLED"}),
    "SUBMITTED": frozenset({"IN_REVIEW", "RETURNED", "REJECTED", "CANCELLED"}),
    "IN_REVIEW": frozenset({"PENDING_INFO", "APPROVED", "REJECTED", "RETURNED", "CANCELLED"}),
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
# **COMPLETE as of branch 2's task 8 — this is the whole of it.** Branch 1
# shipped `get`, `current_calculation` and `set_status`, plus the four event
# names in `applications/events.py`, and said "branch 2 adds `precheck`,
# `submit` and the decision flow". Branch 2 has: tasks 3-7 landed the draft
# lifecycle, the documents and the pre-check, `GET /package` + `submit`,
# `start-review`/`cancel`/`timeline`, and `decision.py`'s
# approve/reject/forward. **None of them widened the surface below** — every
# one is a FLOW VERB reached over HTTP by a human's own client, behind
# `get_current_user` and this module's own ownership and zone rules, and none
# is a function a level-4 module may call. The three functions and the four
# names below are still the entire contract 3.10 and 3.11 build against, and
# they have not changed.
#
# Two additions branch 2 DID make to what a level-4 caller must know, neither
# of them a new entry point:
#
#   * `applications.events.NOTIFIED_EVENT_CODES` — the dotted
#     `notification_templates.event_code` values this module notifies on,
#     beside the four flat bus names. A module ADDING a notification here
#     registers it there; a module reading events wants the four names, not
#     these.
#   * the signature identities below are now PRODUCED, not merely promised:
#     `submit` writes the SUBMITTED history row with `id = <the signed
#     submission id>` and `decision.py` signs the application itself. 3.11
#     collects both through `signatures.service.get_for_object` and must not
#     guess either.
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
#   APPLICATION_APPROVED, APPLICATION_REJECTED, APPLICATION_CANCELLED. All
#   four are PUBLISHED as of branch 2 — `submit` publishes the first,
#   `decision.approve`/`reject` the next two, `cancel` the last — each with
#   `{"application_id": ...}` and nothing else, the payload contract
#   `applications/events.py` froze. `forward` publishes NOTHING: it moves the
#   assignment, not the application, and there is no decision yet to react to.
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
    await _assert_in_actor_zone(db, application, actor=actor, action=APPLICATION_READ)
    return application


async def _assert_in_actor_zone(
    db: AsyncSession, application: Application, *, actor: User, action: str
) -> None:
    """The TERRITORIAL half of a staff rule, on one already-loaded application —
    and the RI-12 trail its refusal owes (`tz/10`: «попытка доступа вне
    территориальных полномочий», High, immediate).

    One body for every staff path in this module (task 6): `_readable_
    application`'s read rule and `start_review`'s write rule ask the identical
    question, and a second copy is a second place to forget that a null
    `assigned_org_id` means "read the contour's owner instead"
    (`_effective_organization`). `action` is the caller's OWN flow-verb constant
    (ruling 17), so the journal says whether the refused attempt was a read or
    an attempt to take the application into work — the refusal itself is
    identical.

    **The refusal is 404 `ERR-SYS-003`, never 403.** Unlike a permit, whose
    existence is already public through the QR check, an application carries a
    citizen's name, plot and herd from the moment it is created, so a 403 would
    make every route on it an application-existence oracle for anybody holding a
    session. Tasks 3–5 answer 404 on every ownership and zone refusal in this
    module and this keeps that one answer.

    The trail is written and COMMITTED before the raise (decision #40 ruling 2):
    the exception would otherwise roll back the very entry that explains it.
    A caller that has uncommitted work of its own on this session must therefore
    treat this as a commit point — `start_review` takes its row lock and calls
    this before writing anything.
    """
    if zone_of(actor) == Zone(None, None, None):
        return
    organization_id = await _effective_organization(db, application)
    if organization_id is not None and await _organization_in_actor_zone(
        db, actor, organization_id
    ):
        return
    await audit.log(
        db,
        action=action,
        user_id=actor.id,
        object_type="application",
        object_id=application.id,
        result="denied",
        basis="out_of_zone",
        extra={"risk_indicator": "RI-12"},
    )
    await db.commit()
    raise err("ERR-SYS-003", details={"application": str(application.id)})


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
    application = await _own_application_for_update(db, application_id, actor=actor)
    if application.status != INITIAL_STATUS:
        raise err("ERR-APP-004", details={"reason": "not_draft", "status": application.status})
    return application


async def _own_application_for_update(
    db: AsyncSession, application_id: uuid.UUID, *, actor: User
) -> Application:
    """The caller's own application, locked, in WHATEVER status it holds — the
    ownership half of `_own_draft_for_update` above, split out by task 6 so
    `cancel` (legal from DRAFT, SUBMITTED and IN_REVIEW alike) shares ONE
    definition of "the caller's own" with the draft routes rather than
    re-deriving it. The status question then belongs to each caller: the draft
    routes want `DRAFT`, `cancel` wants whatever `APPLICATION_TRANSITIONS` says.

    404 for a stranger, never 403 — see `_assert_in_actor_zone`'s note on why
    every refusal in this module is the same answer.
    """
    application = await repo.get_application_for_update(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    if application.applicant_id not in await _own_applicant_ids(db, actor):
        raise err("ERR-SYS-003", details={"application": str(application_id)})
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


# --- Task 4: documents and the pre-check --------------------------------------


async def _own_document_file(db: AsyncSession, file_id: uuid.UUID, *, actor: User) -> MediaFile:
    """The `media_files` row an applicant named, checked before it is stored on
    their application.

    Three conditions, and the third is the one that matters: the row exists, it
    is not archived, and `uploaded_by` is the CALLER. `gis.service.
    _assert_approval_doc_active` stops at the first two because an approval
    decree is scanned by staff and may legitimately be somebody else's upload;
    this is `auth.service._check_poa_file`'s situation instead — a file id an
    APPLICANT supplies, over a table whose ids are guessable in principle, where
    an existence check alone would let anyone hang another citizen's document on
    their own application and put it in front of a reviewer as their evidence.

    No content-type rule, unlike `_check_poa_file`'s PDF: a supporting document
    for a benefit claim is as legitimately a photograph of a certificate as a
    scan of one, and `core.files` already caps what may be uploaded at all.

    `MediaFile` is a core (level 0) model, so reading it here crosses no module
    boundary — the same reason `norms.service._assert_doc_active` reads it
    directly.
    """
    file = await db.get(MediaFile, file_id)
    if file is None or file.status != "active":
        raise err("ERR-VAL-001", details={"reason": "document_file_not_found"})
    if file.uploaded_by != actor.id:
        raise err("ERR-VAL-001", details={"reason": "document_file_not_owned"})
    return file


async def _assert_doc_type(db: AsyncSession, doc_type_item_id: uuid.UUID) -> None:
    """The document type must be an ACTIVE item of the `doc_types` classifier.

    Membership, not mere existence: `classifier_items` holds every classifier's
    values in one table, so an id-only check would accept a rejection reason or
    a benefit category as a document type. An unknown id reaching the INSERT is
    an `IntegrityError` with no handler — `ERR-SYS-001`/500 for a typo (lesson).
    Read through `admin.repo`, never a direct query of `classifier_items`.
    """
    item = await admin_repo.get_classifier_item(db, doc_type_item_id)
    classifier = await admin_repo.get_classifier_by_code(db, DOC_TYPE_CLASSIFIER_CODE)
    if (
        item is None
        or classifier is None
        or item.classifier_id != classifier.id
        or item.status != "active"
    ):
        raise err("ERR-VAL-001", details={"reason": "unknown_doc_type"})


async def add_document(
    db: AsyncSession, application_id: uuid.UUID, payload: ApplicationDocumentIn, *, actor: User
) -> ApplicationDocument:
    """`POST /applications/{id}/documents` — attach an already-uploaded file.

    The owner's own DRAFT only (`_own_draft_for_update`): 404 for a stranger, 409
    for an application that has moved on. Two-step by design — the bytes go
    through `POST /files` first, so this route carries no multipart body, no
    size cap of its own and no storage failure mode; what it stores is a
    reference, checked by `_own_document_file`.
    """
    application = await _own_draft_for_update(db, application_id, actor=actor)
    await _assert_doc_type(db, payload.doc_type_item_id)
    await _own_document_file(db, payload.file_id, actor=actor)
    document = ApplicationDocument(
        application_id=application.id,
        doc_type_item_id=payload.doc_type_item_id,
        file_id=payload.file_id,
        uploaded_by=actor.id,
        note=payload.note,
    )
    await repo.add_document(db, document)
    await audit.log(
        db,
        action=APPLICATION_DOCUMENT_ATTACH,
        user_id=actor.id,
        object_type="application_document",
        object_id=document.id,
        new_value={
            "application_id": str(application.id),
            "doc_type_item_id": str(document.doc_type_item_id),
            "file_id": str(document.file_id),
        },
    )
    return document


async def remove_document(
    db: AsyncSession, application_id: uuid.UUID, document_id: uuid.UUID, *, actor: User
) -> None:
    """`DELETE /applications/{id}/documents/{documentId}` — 204, DRAFT only.

    BOTH ids are checked: a document that belongs to a different application is
    404 rather than a cross-application delete, which is why the path names the
    application at all. The `media_files` row itself survives — files are never
    deleted in this system — so the same file can be re-attached, or attached
    elsewhere, afterwards.
    """
    application = await _own_draft_for_update(db, application_id, actor=actor)
    document = await repo.get_document(db, document_id)
    if document is None or document.application_id != application.id:
        raise err("ERR-SYS-003", details={"document": str(document_id)})
    removed = {
        "application_id": str(application.id),
        "doc_type_item_id": str(document.doc_type_item_id),
        "file_id": str(document.file_id),
    }
    await repo.delete_document(db, document)
    await audit.log(
        db,
        action=APPLICATION_DOCUMENT_DETACH,
        user_id=actor.id,
        object_type="application_document",
        object_id=document_id,
        old_value=removed,
    )


async def precheck(db: AsyncSession, application_id: uuid.UUID, *, actor: User) -> dict[str, Any]:
    """`POST /applications/{id}/precheck` — a dry run: it records what the
    checks said and quotes a price, and moves nothing.

    **A blocking GIS or norm result comes back INSIDE `checks`, as data — never
    as an HTTP error** (design/03, and 3.7's own `calc_router` docstring). The
    applicant has to be able to SEE that the herd is 40 head over the limit, not
    merely be refused; task 5's `submit` runs the identical `checks.run_all` and
    turns the very same result into `first_blocking_error`'s refusal. A broken
    INPUT is still an HTTP error on both paths — an unknown livestock type, a
    reversed period (`norms.checks.run_checks` guards that one fail-closed for
    every caller), a rule parameter that is not published.

    ONE `norms.service.preview` call, whose check list is handed to
    `checks.run_all` (`norm_results=`): a second, independent run would let the
    `checks` an applicant reads and the `calculation` beside them describe two
    different requests, and its `limit` check would be `skipped` rather than the
    real comparison — `norms.service.run_checks` never prices, by design.

    **Nothing is stored of that price** (ruling 8): exactly one `calculations`
    row is ever written, at submission. A speculative row here would be the one
    3.10 builds its invoice from.

    The owner's own DRAFT only, and locked, exactly like a PATCH: this writes
    `application_checks` rows against the application, and a pre-check racing a
    submission would otherwise record evidence for a package that was signed
    without it. A reviewer re-running the checks on a submitted application is
    3.9b's route, not this one.
    """
    application = await _own_draft_for_update(db, application_id, actor=actor)
    priced: dict[str, Any] | None = None
    if not await checks.missing_for_pricing(db, application):
        priced = await norms_service.preview(
            db, payload=await checks.calculation_payload(db, application), actor=actor
        )
    results = await checks.run_all(
        db, application, norm_results=None if priced is None else priced["checks"]
    )
    await audit.log(
        db,
        action=APPLICATION_PRECHECK,
        user_id=actor.id,
        object_type="application",
        object_id=application.id,
        new_value={
            "checks": [{"check_type": row.check_type, "result": row.result} for row in results],
            "priced": priced is not None,
        },
    )
    return {"checks": results, "calculation": priced}


# --- Task 5: the submission ---------------------------------------------------
#
# Fourteen steps, ONE transaction, and the ORDER is the design — not a style.
# `signatures.service.sign()` COMMITS this session on every refusal path (its
# own TRANSACTION CONTRACT docstring, and plan ruling 19), and that commit
# takes everything pending with it. So "the transaction rolls back" is not what
# protects anything here; the ordering is:
#
#   0 mint `submission_id`   8 sign          (the first thing that can commit)
#   1 DRAFT only             9 save_calculation  (only once the ERI is good)
#   2 completeness          10 the public number (never reached by a refusal)
#   3 the benefit document  11 SUBMITTED + the history row `id=submission_id`
#   4 freeze the version    12 audit
#   7 price (preview)       13 notify
#   5 record the checks     14 publish
#   6 refuse on a blocker
#
# Steps 7 and 5 run in that order deliberately (controller ruling R12, and
# `checks.run_all`'s own docstring): `norms.service.run_checks` resolves
# `used_sb=None`, so its `limit` check is permanently `skipped` — an over-limit
# herd would be RECORDED AS UNCHECKED and step 6 would let it through. Handing
# `preview`'s checks in is what makes step 6 mean anything.
#
# What may legitimately be pending when step 8 commits: the `application_checks`
# rows from step 5 and the frozen `contour_version_id`/`requested_area_ha` from
# step 4. Both are the record of a genuine submission attempt (rulings 12 and
# 19). A stored calculation is NOT — hence step 9's position.

# Ruling 25: a SUBMISSION is signed against the ATTEMPT, never against the
# application. `uq_signatures_valid_purpose` is UNIQUE on
# `(object_type, object_id, purpose)` where the row is valid, so signing
# `("application", <application id>, ...)` would let an application be
# submitted exactly once EVER and dead-end 3.9b's return-for-correction on
# `ERR-SIGN-002`. Each attempt is its own `object_id`, so each may hold one
# valid signature and a resubmission collides with nothing.
SUBMISSION_OBJECT_TYPE = "application_submission"
SUBMISSION_PURPOSE = "application_submit"
# Ruling 17, beside the constants above: a flow verb audits under its own name.
APPLICATION_SUBMIT = "application.submit"
# `notification_templates.event_code`, seeded by migration 0009 — DOTTED, and a
# THIRD vocabulary beside the audit action above and `events.APPLICATION_
# SUBMITTED` (flat) below. `applications/events.py` carries the table. Passing
# the bus name here would find no template, and `notify()` answers that by
# writing a raw fallback string in-app and sending NOTHING by SMS or e-mail,
# silently, on every single submission.
NOTIFY_APPLICATION_SUBMITTED = "application.submitted"
SUBMITTED_STATUS = "SUBMITTED"
# Ruling 5а: `RX-<year>-<seq>` out of `number_counters`, scope `RX:<year>`.
NUMBER_PREFIX = "RX"
# Ruling 13: `sla_deadline_at = submitted_at + 15 days`, stored at submission
# because it is part of the submission's record. The reminders, the RI-07
# escalation and the pause arithmetic across `info_requests` are all 3.9b's,
# which owns PENDING_INFO.
SLA_DAYS = 15


def _canonical_decimal(value: Decimal | str) -> str:
    """One rendering of a decimal for the signed package, and the reason
    `_package_bytes` can take either shape of price (controller ruling R2).

    A fixed-scale NUMERIC round-trips at the COLUMN's own scale, not the
    calculator's (lesson): the same money is `"2060000.00"` out of
    `norms.service.preview` and `Decimal('2060000.0000')` off the stored
    `calculations` row. Trailing zeros are stripped so the two are one string.

    `format(..., "f")` first, never `Decimal.normalize()`: normalize renders a
    whole number in scientific notation (`Decimal('100.0000').normalize()` is
    `Decimal('1E+2')`), which would change the signed bytes for round amounts
    only — the worst possible failure to notice.
    """
    text = format(Decimal(str(value)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _package_bytes(
    application: Application,
    priced: Any,
    *,
    contour_version_id: uuid.UUID | None = None,
) -> bytes:
    """The canonical bytes an applicant signs: applicant, activity, contour
    version, period, items, quantity, the priced amount and the rule version.
    Sorted keys, no whitespace, UTF-8, ONE function.

    This is what a verifier re-derives years later from the stored row. A
    change to key order, to which fields are included, or to how a `Decimal` is
    rendered silently invalidates every signature ever produced — which is why
    `test_submit.py` pins the exact byte string rather than merely round-
    tripping it.

    **`priced` is EITHER `norms.service.preview`'s dict OR a stored
    `Calculation` row, and the two MUST produce identical bytes** (controller
    ruling R2). They are not interchangeable by accident: ruling 19 forces
    `submit` to sign the dict, because nothing is stored yet at signing time,
    while a verifier has only the row. Both are normalised here — the amount
    through `_canonical_decimal`, everything else out of `input_snapshot`,
    which both carry in the same shape.

    **RULING 23 — a stale package is an accepted 3.9a exposure, and this is the
    function it starts in.** The amount comes from `norms.service.preview`,
    which prices at `business_today()` against whatever tariffs and БҲМ are
    effective right then. So a tariff or `rule_parameter` published between the
    `GET /package` and the `POST /submit`, a norm published or archived, or
    plain midnight in Tashkent, changes these bytes — and the applicant then
    meets `ERR-SIGN-001` for something they did not do. Oybek chose option (в)
    on 2026-09-02: leave it, and fix it in 3.9b with the whole review flow in
    view. Do NOT "fix" it here by caching the package or by dropping the
    amount from it; both are 3.9b's call to make.

    `contour_version_id` is supplied by `GET /package`, whose application is
    still a DRAFT and has not frozen the column yet (step 4 does that, at
    submission). It must be the SAME published version the submission will
    freeze, or the two calls produce different bytes; both resolve it through
    `gis.service.published_version`.
    """
    version_id = contour_version_id or application.contour_version_id
    if version_id is None:
        # Never an `assert` on a request path — `-O` strips it, and what this
        # function returns is SIGNED (review round 2, minor 9). Unreachable
        # today: both callers run `_published_version_or_refuse` first, which
        # is the honest 409 for a contour whose geometry is still a draft.
        raise err("ERR-SYS-001", details={"reason": "package_without_a_contour_version"})
    if isinstance(priced, Mapping):
        amount = priced["amount"]
        rule_version = priced["rule_code_version"]
        snapshot = priced["input_snapshot"]
    else:
        amount = priced.amount
        rule_version = priced.rule_code_version
        snapshot = priced.input_snapshot
    request = snapshot["request"]
    package = {
        "activity_type_code": request["activity_code"],
        "amount": _canonical_decimal(amount),
        "applicant_id": str(application.applicant_id),
        # **WHICH application this is** (review round 2, important 3). Without
        # it two drafts of one applicant with identical content produce
        # identical bytes, so one PKCS#7 verifies for either — and "I signed
        # THIS application" is exactly what a non-repudiation document must be
        # able to prove from the bytes alone.
        #
        # `submission_id` is deliberately NOT here: it is minted per attempt at
        # `submit`'s step 0, and `GET /package` cannot know it, so including it
        # would make every fetched package differ from the bytes `submit` signs
        # and every submission would fail `ERR-SIGN-001`. The attempt is
        # identified by the signature's own `object_id` instead (ruling 25).
        "application_id": str(application.id),
        "contour_version_id": str(version_id),
        # Sorted by code, never in the order the rows happened to arrive: a herd
        # re-sent in a different order is the same herd, and the bytes have to
        # say so.
        "items": sorted(
            (
                {"livestock_code": item["livestock_code"], "count": item["count"]}
                for item in request["items"]
            ),
            key=lambda item: item["livestock_code"],
        ),
        "period_from": None
        if application.period_from is None
        else application.period_from.isoformat(),
        "period_to": None if application.period_to is None else application.period_to.isoformat(),
        "quantity": None
        if application.quantity is None
        else _canonical_decimal(application.quantity),
        "rule_version": rule_version,
    }
    return json.dumps(package, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


async def _assert_complete(db: AsyncSession, application: Application) -> None:
    """Step 2. Every field a submission needs, or 400 `ERR-APP-001` NAMING the
    ones that are missing.

    **400, not 422** — the catalogue, `tz/10` and the original spec all say so
    for `ERR-APP-001`, and it is the honest status: the request body is fine,
    the application is not finished.

    `checks.missing_for_pricing` is the one definition, shared with the
    pre-check so the two can never disagree about what "ready to file" means —
    a pre-check that said "ready" and a submission that then refused would make
    that route useless at the one thing it exists for. `applicant_id` is absent
    from the list because the column is NOT NULL and cannot be missing.
    """
    missing = await checks.missing_for_pricing(db, application)
    if missing:
        raise err("ERR-APP-001", details={"missing": missing})


async def _benefit_doc_type(db: AsyncSession) -> Any:
    """The ACTIVE `doc_types` item whose code is `BENEFIT_DOC_TYPE_CODE`, or
    `None` when the Agency has not seeded it yet.

    Read through `admin.repo`, never a query of `classifier_items` here
    (CLAUDE.md: reference data is read-only and reached through its owner).
    `list_classifier_items` already applies "valid today AND active", which is
    the only sense in which a doc type is usable on a submission — an archived
    or not-yet-valid one is not proof of anything.
    """
    classifier = await admin_repo.get_classifier_by_code(db, DOC_TYPE_CLASSIFIER_CODE)
    if classifier is None:
        return None
    items = await admin_repo.list_classifier_items(db, classifier.id)
    return next((item for item in items if item.code == BENEFIT_DOC_TYPE_CODE), None)


async def _assert_benefit_documents(db: AsyncSession, application: Application) -> None:
    """Step 3, ruling 10а: a claimed benefit needs a supporting document of the
    BENEFIT type (`tz/06` § Льготы — «Реестр льготных категорий +
    подтверждающие документы»; `tz/04` С3 item 9). 422 `ERR-APP-003`,
    «неполный комплект документов», which is exactly what this is.

    **FAIL-CLOSED, and deliberately so** (review round 2, important 5). A
    benefit REDUCES the fee, so "any attachment will do" is a fee-reducing
    claim accepted on evidence nobody checked. The project's posture on
    benefits is fail-closed everywhere else — `ERR-NORM-004` refuses a grazing
    fee outright rather than guessing a missing `coef_sb:*`, and
    `benefit_categories` ships EMPTY so no benefit can be claimed at all today
    — and this now matches it:

      * the document must be of the `doc_types` item whose code is
        `BENEFIT_DOC_TYPE_CODE` below; a document of any other type does not
        satisfy the claim;
      * if that classifier item does not exist or is not active — which is the
        state of a fresh database, since migration 0005 seeds the `doc_types`
        CLASSIFIER but none of its ITEMS, those being the Agency's to supply —
        the claim is REFUSED with `benefit_doc_type_not_configured`, never
        accepted. An unconfigurable rule refuses; it does not wave things
        through.

    The consequence, stated plainly: until the Agency seeds `benefit_proof` in
    `doc_types`, no benefit can be claimed on a submission. That is the same
    fail-closed state `tz/12` #2 already describes for the benefit list itself,
    and the controller is recording it as an open question.

    The claim is separately validated against the benefit classifier at PATCH
    time (`_assert_references`) and against the tariff rows it must resolve at
    pricing time (decision #50), so an invented category never gets this far.
    """
    if application.benefit_category_item_id is None:
        return
    doc_type = await _benefit_doc_type(db)
    if doc_type is None:
        raise err(
            "ERR-APP-003",
            details={
                "reason": "benefit_doc_type_not_configured",
                "doc_type_code": BENEFIT_DOC_TYPE_CODE,
            },
        )
    documents = await repo.list_documents(db, application.id)
    if not any(document.doc_type_item_id == doc_type.id for document in documents):
        raise err(
            "ERR-APP-003",
            details={
                "reason": "benefit_claim_needs_a_document",
                "doc_type_code": BENEFIT_DOC_TYPE_CODE,
                "benefit_category_item_id": str(application.benefit_category_item_id),
            },
        )


async def _published_version_or_refuse(db: AsyncSession, application: Application) -> Any:
    """Step 4's half that can fail: the contour's version currently in force.

    A contour whose geometry is still a draft has nothing to submit against —
    no geometry to check, and no `area_ha` to freeze. 409 `ERR-GIS-005`, gis's
    own state-conflict code: the contour exists, its GEOMETRY is in the wrong
    state. `checks._gis_results` reports the same situation as `skipped` rows
    on the pre-check path, which is why it cannot be left to `first_blocking_
    error` — a `skipped` check blocks nothing.
    """
    assert application.contour_id is not None  # `_assert_complete` ran first
    version = await gis_service.published_version(db, application.contour_id)
    if version is None:
        raise err(
            "ERR-GIS-005",
            details={"reason": "no_published_version", "contour_id": str(application.contour_id)},
        )
    return version


async def _price(
    db: AsyncSession, application: Application, *, actor: User
) -> tuple[CalculationIn, dict[str, Any]]:
    """The `norms` request this application describes, and what `preview` says
    it costs — WRITTEN NOWHERE (ruling 19).

    The request is returned alongside the price so `submit` can hand the SAME
    object to `save_calculation` at step 9 instead of rebuilding it: two builds
    are two chances for the amount that was signed and the amount that is
    stored to describe different requests, and `checks.calculation_payload`
    reads the database each time.

    `preview` and `save_calculation` share one `norms.service._compute` by
    construction, so within one transaction the two cannot disagree.
    """
    payload = await checks.calculation_payload(db, application)
    return payload, await norms_service.preview(db, payload=payload, actor=actor)


async def _notification_recipient(db: AsyncSession, application: Application) -> uuid.UUID:
    """Who hears that this application was filed: the individual applicant's
    own account when there is one, otherwise whoever filed it. A legal entity
    has no `owner_user_id` (decision #9) and is reached through the
    representative who acted for it — which is also the fallback for an
    applicant row that has somehow lost its account, since `notify` RAISES on a
    recipient it cannot resolve and a submission must not fail over a message.
    Same shape as `permits.service._notification_recipient`."""
    applicant = await auth_service.get_applicant(db, application.applicant_id)
    if applicant is not None and applicant.owner_user_id is not None:
        return applicant.owner_user_id
    return application.submitted_by_user_id


async def package(db: AsyncSession, application_id: uuid.UUID, *, actor: User) -> bytes:
    """`GET /applications/{id}/package` — the canonical bytes to be signed.

    The client signs exactly these and nothing else: a detached PKCS#7 cannot
    be produced over bytes the client has never seen, which is why this route
    exists at all (design/03 missed it; task 9 adds it there).

    Owner or staff in zone, through `_readable_application` — so a stranger is
    told 404, never 403: this response carries the applicant, the plot and the
    price.

    **RULING 23 applies here, in full.** This route prices through
    `norms.service.preview` at `business_today()`, exactly as `submit` does a
    moment later, and NOTHING freezes the answer in between. A tariff or
    `rule_parameter` published between the two calls, a norm published or
    archived, or midnight in Tashkent, changes the bytes — and the applicant
    signs one package while the server verifies against another, meeting
    `ERR-SIGN-001` for something they did not do. Accepted for 3.9a (Oybek's
    choice, option в, 2026-09-02) and 3.9b's to fix; do not cache the package
    or drop the amount from it here.
    """
    application = await _readable_application(db, application_id, actor=actor)
    await _assert_complete(db, application)
    version = await _published_version_or_refuse(db, application)
    _, priced = await _price(db, application, actor=actor)
    return _package_bytes(application, priced, contour_version_id=version.id)


async def submit(
    db: AsyncSession, application_id: uuid.UUID, *, pkcs7: str, actor: User
) -> Application:
    """`POST /applications/{id}/submit` — the fourteen steps of the block
    comment above, in one transaction.

    **RULING 23, restated at the third of its three required places.** The
    package is priced afresh HERE, and the bytes the client signed came from a
    separate `GET /package` call priced at its own moment. A tariff, a
    `rule_parameter`, a norm or the Tashkent date moving in between makes the
    two disagree and the applicant meets `ERR-SIGN-001` for something they did
    not do. Accepted for 3.9a; 3.9b decides between freezing the package and
    dropping the amount from it.
    """
    # Step 0. Minted before anything is written, because it is what step 8
    # signs and what step 11 stores as the history row's primary key (ruling
    # 25) — and minting it first is what lets a signature exist for a REFUSED
    # attempt whose history row is never written. That orphan is the evidence
    # trail working, not a leak: `sign()` commits its refusal, nothing else is
    # written, and the row says "an attempt was made against submission X and
    # it was rejected".
    submission_id = uuid7()
    # Step 1. The owner's own DRAFT, locked: 404 for a stranger, 409
    # `ERR-APP-004` for an application that has moved on. 3.9b adds RETURNED.
    application = await _own_draft_for_update(db, application_id, actor=actor)
    await _assert_complete(db, application)  # step 2
    await _assert_benefit_documents(db, application)  # step 3
    # Step 4, ruling 22: the geometry decided upon AND its area, frozen
    # together because they are one fact. Without the second,
    # `max_approve_area` (decision #29) compares against NULL for the rest of
    # this application's life and never fires once.
    version = await _published_version_or_refuse(db, application)
    application.contour_version_id = version.id
    application.requested_area_ha = version.area_ha

    payload, priced = await _price(db, application, actor=actor)  # step 7, BEFORE step 5 (R12)
    results = await checks.run_all(db, application, norm_results=priced["checks"])  # step 5
    blocking = checks.first_blocking_error(results)  # step 6
    if blocking is not None:
        # Here, unlike the pre-check, a blocking result IS an error — the whole
        # difference between the two routes, and the reason they share
        # `run_all`. The rows are kept either way (ruling 12).
        raise blocking

    # Step 8. The first thing on this page that can commit — see ruling 19 and
    # `sign()`'s own TRANSACTION CONTRACT. Everything pending right now is
    # evidence of a genuine attempt and is right to keep; nothing else may be.
    await signatures_service.sign(
        db,
        object_type=SUBMISSION_OBJECT_TYPE,
        object_id=submission_id,
        purpose=SUBMISSION_PURPOSE,
        document=_package_bytes(application, priced, contour_version_id=version.id),
        pkcs7=pkcs7,
        user=actor,
    )

    # Step 9, ruling 8: EXACTLY ONE calculation per application, written here
    # and nowhere earlier — 3.10 builds its invoice from the newest row, so a
    # speculative one written before the signature was known good would be a
    # live under-billing row in an append-only table.
    calculation = await norms_service.save_calculation(
        db,
        # The SAME request that was priced and signed a moment ago, with the
        # binding added — `model_copy` rather than a second
        # `calculation_payload` build, so the stored row and the signed package
        # cannot describe two different herds.
        payload=payload.model_copy(update={"application_id": application.id}),
        actor=actor,
    )
    # Step 10, ruling 5а. AFTER the signature, so a refused ERI never reaches
    # the counter at all; inside the transaction, so a failure later than this
    # rolls the counter back with it and the year's numbering has no holes.
    number = await next_public_number(db, NUMBER_PREFIX, business_today())

    submitted_at = datetime.now(UTC)
    # The duplicate's key, read BEFORE the savepoint and never after it. A
    # `begin_nested()` ROLLBACK expires every instance that was dirty inside the
    # savepoint (`SessionTransaction._restore_snapshot`), so `application.
    # applicant_id` in the `except` below would be a lazy reload — which on an
    # async session raises `MissingGreenlet` from inside an exception handler,
    # turning this clean 409 into a 500. The brief's snippet read them off the
    # row in the handler; it cannot.
    clash_applicant_id = application.applicant_id
    clash_contour_id = application.contour_id
    clash_activity_type_id = application.activity_type_id
    clash_period_from = application.period_from
    clash_period_to = application.period_to
    clash_exclude_id = application.id
    try:
        # Step 11. `begin_nested()` (a SAVEPOINT), not a bare flush: the
        # duplicate surfaces as an `IntegrityError` on THIS update, and the
        # session has to stay usable afterwards to look the clash up and answer
        # cleanly. A bare flush caught by `except IntegrityError` rolls back the
        # WHOLE transaction — every check row and the frozen version with it.
        async with db.begin_nested():
            application.status = SUBMITTED_STATUS
            application.number = number
            application.submitted_at = submitted_at
            application.sla_deadline_at = submitted_at + timedelta(days=SLA_DAYS)
            await db.flush()
    except IntegrityError as exc:
        # `IntegrityError` IS a `DBAPIError` subclass and this narrow clause
        # must come first; and the constraint is named through
        # `exc.orig.__cause__` (asyncpg's own exception), never by matching the
        # message — SQLAlchemy's `exc.orig` is only a thin DBAPI wrapper and
        # exposes `pgcode`/`sqlstate` alone (lesson). A bare `except
        # IntegrityError` would swallow `applications.number`'s UNIQUE index
        # and every FK on this table and report all of them as "duplicate".
        cause = exc.orig.__cause__ if exc.orig is not None else None
        if getattr(cause, "constraint_name", None) != "ex_applications_no_duplicate":
            raise
        # Ruling 6: the EXCLUDE constraint is the only detector — a "check then
        # insert" is a race two clicks a millisecond apart both win.
        # `active_overlapping` re-runs the constraint's own predicate as a
        # SELECT for the sole purpose of NAMING the colliding application, so
        # the applicant is told WHICH filing already covers this period.
        clash = await repo.active_overlapping(
            db,
            applicant_id=clash_applicant_id,
            contour_id=clash_contour_id,
            activity_type_id=clash_activity_type_id,
            period_from=clash_period_from,
            period_to=clash_period_to,
            exclude_id=clash_exclude_id,
        )
        if clash is None:
            # The constraint fired and the predicate finds nothing: whatever
            # that is, it is not the duplicate this branch explains. Surface it
            # as itself rather than inventing a number for the error body.
            raise
        raise err("ERR-APP-002", details={"existing_number": clash.number}) from exc

    # The history row carries `id = submission_id` (ruling 25) so the step-8
    # signature resolves to the exact transition it belongs to, with no join
    # table. This is also why `submit` writes its own transition instead of
    # routing through `set_status`, whose frozen signature cannot express it.
    await repo.add_status_history(
        db,
        ApplicationStatusHistory(
            id=submission_id,
            application_id=application.id,
            from_status=INITIAL_STATUS,
            to_status=SUBMITTED_STATUS,
            changed_by=actor.id,
        ),
    )
    # `updated_at` is `onupdate=func.now()`, which SQLAlchemy leaves EXPIRED
    # after a plain UPDATE, and `requested_area_ha` round-trips at
    # NUMERIC(12,4)'s own scale rather than the version's (lesson) — both are
    # in this response.
    await db.refresh(application)

    await audit.log(  # step 12, ruling 17: the flow verb's own constant
        db,
        action=APPLICATION_SUBMIT,
        user_id=actor.id,
        object_type="application",
        object_id=application.id,
        old_value={"status": INITIAL_STATUS},
        new_value={
            "status": SUBMITTED_STATUS,
            "number": number,
            "submission_id": str(submission_id),
            "contour_version_id": str(version.id),
            "requested_area_ha": _json_safe(application.requested_area_ha),
            "calculation_id": str(calculation.id),
        },
    )
    await notifications_service.notify(  # step 13 — DOTTED, see the constant
        db,
        event_code=NOTIFY_APPLICATION_SUBMITTED,
        recipient_user_id=await _notification_recipient(db, application),
        params={"application_number": number},
        object_type="application",
        object_id=application.id,
    )
    # Step 14. `application_id` and NOTHING else — not `number`, however
    # convenient: `applications/events.py`'s payload contract is frozen, and a
    # subscriber reads everything else through `service.get` /
    # `current_calculation`. Handlers run synchronously, in THIS transaction.
    await publish(db, Event(name=APPLICATION_SUBMITTED, payload={"application_id": application.id}))
    return application


# --- Task 6: taking into work, cancelling, and the timeline -------------------
#
# Two flow verbs and one read. What the two verbs have in common — a locked row,
# `_assert_transition`, a history row and ONE audit entry under the verb's own
# name — lives in `_apply_transition` below, so task 7's `approve`/`reject` add
# their decision-specific work rather than a fourth copy of the transition
# mechanics.
#
# **Two things design/03 asks of these routes that 3.9a deliberately does NOT
# do**, named here so the difference is a decision and not an omission:
#
#   * design/03's `start-review` says «an incomplete package is returned
#     immediately with RJ-01». Returning needs the `RETURNED` status and the
#     return route, both stage 3.9b's (ruling 2), so 3.9a's `start-review`
#     checks status and zone only and an incomplete package reaches a human.
#   * design/03's timeline includes `info_requests`. The table exists from task
#     1 and nothing writes it until 3.9b, so the key is present and EMPTY rather
#     than absent — 3.9b then widens data, not a contract.

# Ruling 17, beside `APPLICATION_SUBMIT` above: a flow verb audits under its own
# name. `APPLICATION_STATUS_CHANGE` belongs to `set_status` — the level-4
# surface — and says only that a status moved, never why.
APPLICATION_START_REVIEW = "application.start_review"
APPLICATION_CANCEL = "application.cancel"

IN_REVIEW_STATUS = "IN_REVIEW"
CANCELLED_STATUS = "CANCELLED"

# Ruling 25's OTHER half, beside `SUBMISSION_OBJECT_TYPE`/`SUBMISSION_PURPOSE`:
# a DECISION is signed as `("application", <the application id>,
# "application_decision")` — one such object per application, however many
# submission attempts it took. Declared here because the TIMELINE reads them
# today; task 7 signs with them.
DECISION_OBJECT_TYPE = "application"
DECISION_PURPOSE = "application_decision"

# `application_assignments.reason` (`models.ASSIGNMENT_REASONS`). 3.9a has no
# auto-assignment job — ruling 14 lets any reviewer in the zone pick an
# application up — so every row this stage writes records a human act. `auto` is
# 3.9b's, when the assignment is made for the reviewer instead of by them.
ASSIGNMENT_MANUAL = "manual"


async def _apply_transition(
    db: AsyncSession,
    application: Application,
    *,
    to_status: str,
    action: str,
    actor: User,
    reason: str | None = None,
    reason_item_id: uuid.UUID | None = None,
    legal_basis: str | None = None,
) -> ApplicationStatusHistory:
    """Move an ALREADY-LOCKED application one legal edge, and leave the two
    records every transition owes behind: the `application_status_history` row
    and one `audit_log` entry under the CALLER'S flow verb (ruling 17).

    `reason_item_id`/`legal_basis` are task 7's rejection grounds (`tz/04` С8)
    and are set HERE, before the insert, never on the returned row: migration
    0015's BEFORE UPDATE trigger makes `application_status_history` append-only,
    so a caller that filled them in afterwards would raise instead of
    recording them.

    Deliberately not `set_status`: that function is the level-4 public surface
    and audits every move as `application.status_change`, which cannot say
    whether an application reached IN_REVIEW because a hodim took it into work
    or CANCELLED because the citizen withdrew. `submit` writes its own
    transition for a different reason again (ruling 25's explicit history-row
    id) — this helper is what stops the third and fourth copies.

    The caller supplies the locked row because the lock is where the ownership
    or zone rule was decided: `_own_application_for_update` and `start_review`
    each lock, check their own rule, and only then arrive here.

    Returns the history row, so a caller that has to bind something to that
    exact transition (ruling 25's signature identity, 3.9b's return reasons) can
    without re-reading it.
    """
    _assert_transition(application, to_status)
    from_status = application.status
    application.status = to_status
    entry = ApplicationStatusHistory(
        application_id=application.id,
        from_status=from_status,
        to_status=to_status,
        changed_by=actor.id,
        reason_text=reason,
        reason_item_id=reason_item_id,
        legal_basis=legal_basis,
    )
    await repo.add_status_history(db, entry)
    # `updated_at` is `onupdate=func.now()`, which SQLAlchemy leaves EXPIRED
    # after a plain UPDATE (lesson: the row in memory is not what Postgres
    # stored) — and every caller here serializes this row into its response.
    await db.refresh(application)
    new_value: dict[str, Any] = {"status": to_status}
    if reason_item_id is not None:
        new_value["reason_item_id"] = str(reason_item_id)
    if legal_basis is not None:
        new_value["legal_basis"] = legal_basis
    await audit.log(
        db,
        action=action,
        user_id=actor.id,
        object_type="application",
        object_id=application.id,
        old_value={"status": from_status},
        new_value=new_value,
        basis=reason or legal_basis,
    )
    return entry


async def _claim_assignment(
    db: AsyncSession,
    application: Application,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    reason: str,
    actor: User,
) -> ApplicationAssignment:
    """Supersede whatever active assignment the application has and record the
    new one — the ONE write path into `application_assignments`.

    `uq_application_assignments_active` is UNIQUE on `(application_id) WHERE
    is_active`, so a blind second insert is an `IntegrityError`, and the flush
    between the deactivation and the insert is NOT optional: without it both
    rows are pending when the index is checked and the insert fails on a
    conflict the flush would have resolved (lesson: "A partial unique index
    constrains only the rows it covers, and only after a flush").

    Built as a supersede from the first caller on purpose, though 3.9a's only
    caller finds nothing to supersede: task 7's forward writes a SECOND row
    pointing at the parent organization, and 3.9b turns `start_review` into a
    claim over a row an auto-assignment job wrote. Both are this function with
    different arguments, and neither is a special case.
    """
    await repo.deactivate_assignments(db, application.id)
    await db.flush()
    row = ApplicationAssignment(
        application_id=application.id,
        org_id=org_id,
        user_id=user_id,
        assigned_by=actor.id,
        reason=reason,
        is_active=True,
    )
    await repo.add_assignment(db, row)
    # `created_at` is a `server_default` the INSERT leaves unloaded, and the
    # timeline both sorts on it and serializes it.
    await db.refresh(row)
    return row


async def start_review(db: AsyncSession, application_id: uuid.UUID, *, actor: User) -> Application:
    """`POST /applications/{id}/start-review` — SUBMITTED -> IN_REVIEW, plus the
    `application_assignments` row that says who holds it.

    **Ruling 14: any reviewer holding `applications.review` whose zone covers
    the application may take it.** There is no auto-assignment job in 3.9a and
    no "the assigned executor" to be, so the route's `require_permission` and
    the zone check below are the whole of the rule — the permission answers "may
    this role at all", the zone answers "on whose rows", and a read or write path
    needs BOTH (lesson).

    The order of the refusals is what the tests pin: a stranger to the zone is
    404 before anything about the status is revealed, and only then is a
    non-SUBMITTED application a 409 `ERR-APP-004`. Reversing them would tell an
    out-of-zone caller which applications exist and what state they are in.

    Locked from the start (`repo.get_application_for_update`): this is a
    read-check-write over `status`, and two hodims clicking «принять в работу» a
    millisecond apart would otherwise both pass `_assert_transition`, both write
    a history row and race on the partial unique index in `_claim_assignment`.
    The second now blocks, re-reads `IN_REVIEW` and gets a clean 409.

    `assigned_org_id` is set to the application's EFFECTIVE organization — the
    contour's owner, since nothing has assigned it yet — and not to the actor's
    own `organization_id`, which is null for a region-scoped reviewer and would
    silently move the application out of everyone's zone. `assigned_user_id` is
    the actor: they took it.
    """
    application = await repo.get_application_for_update(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    # Before the status check, and before anything is written: this commits its
    # RI-12 trail and raises 404 on a territorial refusal.
    await _assert_in_actor_zone(db, application, actor=actor, action=APPLICATION_START_REVIEW)
    _assert_transition(application, IN_REVIEW_STATUS)

    organization_id = await _effective_organization(db, application)
    if organization_id is None:
        # `application_assignments.org_id` is NOT NULL and there is nothing to
        # put in it. Unreachable from SUBMITTED — `_assert_complete` makes
        # `contour_id` mandatory at submission — but a 409 naming the fact beats
        # an IntegrityError/500 the day some other path reaches here.
        raise err(
            "ERR-APP-004",
            details={"reason": "no_organization", "status": application.status},
        )

    application.assigned_org_id = organization_id
    application.assigned_user_id = actor.id
    await _claim_assignment(
        db,
        application,
        org_id=organization_id,
        user_id=actor.id,
        reason=ASSIGNMENT_MANUAL,
        actor=actor,
    )
    await _apply_transition(
        db,
        application,
        to_status=IN_REVIEW_STATUS,
        action=APPLICATION_START_REVIEW,
        actor=actor,
    )
    return application


async def cancel(
    db: AsyncSession, application_id: uuid.UUID, *, reason: str | None = None, actor: User
) -> Application:
    """`POST /applications/{id}/cancel` — the applicant withdraws.

    Legal from DRAFT, SUBMITTED and IN_REVIEW (`APPLICATION_TRANSITIONS`, whose
    last two edges controller ruling R20 added for exactly this): `tz/05` lets
    an applicant withdraw at any point before a decision, and one who no longer
    wants the permit should not have to wait for one. Anything later is
    somebody else's money or somebody else's document, and `_assert_transition`
    refuses it as `ERR-APP-004`.

    The OWNER only — `_own_application_for_update`, so a stranger is told 404
    and never that the application exists. Staff cancellation is not a thing:
    a reviewer who does not want to grant an application REJECTS it (task 7),
    which carries a legal ground; «отменено» is the citizen's own word.

    Publishing `APPLICATION_CANCELLED` matters beyond this stage: 3.10a
    subscribes to it and cancels any in-force invoice, silently and idempotently
    when there is none (`payments.subscribers.on_application_cancelled`). Its
    handler runs synchronously in THIS transaction (ruling 3а), and the payload
    is `application_id` and nothing else — the frozen contract in
    `applications/events.py`.
    """
    application = await _own_application_for_update(db, application_id, actor=actor)
    await _apply_transition(
        db,
        application,
        to_status=CANCELLED_STATUS,
        action=APPLICATION_CANCEL,
        actor=actor,
        reason=reason,
    )
    await publish(db, Event(name=APPLICATION_CANCELLED, payload={"application_id": application.id}))
    return application


async def timeline(db: AsyncSession, application_id: uuid.UUID, *, actor: User) -> dict[str, Any]:
    """`GET /applications/{id}/timeline` — the transitions, the assignments, the
    signatures and (from 3.9b) the information requests.

    Owner or staff in zone, through `_readable_application`: a stranger is told
    404, never 403, because this response says who applied for what and when.

    **The signatures are TWO lookups, not one (ruling 25)**, and getting it
    wrong shows up as an empty `signatures[]` that no assertion about statuses
    or assignments would catch:

      * the DECISION is signed against the APPLICATION —
        `("application", <application id>)`, at most one per application
        however many times it was submitted. It is returned at the TOP level,
        because it belongs to the application rather than to any one row of its
        history;
      * a SUBMISSION is signed against the ATTEMPT —
        `("application_submission", <the SUBMITTED history row's id>)`. `submit`
        supplies that id explicitly (ruling 25) precisely so a signature
        resolves to the exact transition it belongs to with no join table, so
        each one is attached to ITS OWN entry.

    3.9a produces at most one SUBMITTED row; 3.9b's return-and-resubmit produces
    several, each with its own signature, which is why the loop below is a loop
    over every SUBMITTED row rather than a lookup of "the" submission.

    Both are read through `signatures.service.get_for_object` — never by
    querying that module's table (module boundary, CLAUDE.md).

    `info_requests` is `[]` and present: the table exists and nothing writes it
    before 3.9b, so shipping the key now means 3.9b widens the DATA and not the
    contract.
    """
    application = await _readable_application(db, application_id, actor=actor)
    history = await repo.list_status_history(db, application.id)
    entries: list[dict[str, Any]] = []
    for entry in history:
        signatures = (
            await signatures_service.get_for_object(
                db, object_type=SUBMISSION_OBJECT_TYPE, object_id=entry.id
            )
            if entry.to_status == SUBMITTED_STATUS
            else []
        )
        entries.append({"entry": entry, "signatures": signatures})
    return {
        "status_history": entries,
        "assignments": await repo.list_assignments(db, application.id),
        "signatures": await signatures_service.get_for_object(
            db, object_type=DECISION_OBJECT_TYPE, object_id=application.id
        ),
        # 3.9b's, and empty by contract until then — see the docstring.
        "info_requests": [],
    }
