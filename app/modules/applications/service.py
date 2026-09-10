"""Applications service — business logic over the `applications` tables
(design/02 § applications; plan `03.9a-applications-core`).

**Stage 3.9a is complete as of this branch.** The file carries the whole
applicant-and-staff flow: the draft's own four routes, `precheck` and the
documents, `GET /package` and `submit` (with the duplicate guard and the
public number), `start_review`, `cancel` and `timeline`. The head's decision —
`approve`, `reject` and the over-limit `forward` — lives beside it in
`decision.py`, which imports this module rather than the other way round.

What is NOT here is 3.9b's, and its absence is deliberate rather than
pending-in-this-file: `return_to_applicant`, `request_info` and the
`RETURNED`/`PENDING_INFO` states they produce, and the assignment routes. See
the "Task 8 public surface" comment below for the contract this file promises
levels 4+ (payments 3.10, permits 3.11) — three functions and four event
names, unchanged since branch 1."""

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Row, and_, or_
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
from app.modules.admin import open_work as admin_open_work
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import ClassifierItem, Organization
from app.modules.applications import checks, repo, sla
from app.modules.applications.assignment import choose_executor
from app.modules.applications.events import APPLICATION_CANCELLED, APPLICATION_SUBMITTED
from app.modules.applications.models import (
    APPLICATION_KINDS,
    Application,
    ApplicationAssignment,
    ApplicationCheck,
    ApplicationConclusion,
    ApplicationDocument,
    ApplicationItem,
    ApplicationStatusHistory,
    InfoRequest,
)
from app.modules.applications.permissions import (
    APPLICATIONS_CONCLUDE_GIS,
    APPLICATIONS_DECIDE,
    APPLICATIONS_REVIEW,
    APPLICATIONS_VIEW_ANY,
)
from app.modules.applications.schemas import (
    ApplicationCheckIn,
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
from app.modules.integrations import service as integrations_service
from app.modules.integrations.adapters import cadastre as cadastre_adapter
from app.modules.integrations.adapters import vet as vet_adapter
from app.modules.integrations.adapters.cadastre import CadastreCheckResult
from app.modules.integrations.adapters.vet import VetCheckResult
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
# What `decision._forward` writes into the bounce row's `reason_text`
# (controller minor 4) — defined HERE, not in `decision.py`, because ruling
# #107's `_readable_application` below needs the same string and `decision.py`
# imports THIS module (`as flow`), never the other way: `decision.py` reads it
# back as `flow.FORWARD_REASON`, exactly like `STALE_PACKAGE_REASON`/
# `ASSIGNMENT_MANUAL` below. A STABLE TOKEN, never a sentence — `reason_text`
# surfaces on the citizen-visible timeline, and this project keeps user-facing
# wording in versioned `notification_templates` rows an admin owns, never in
# code.
FORWARD_REASON = "role_limit_exceeded"
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
# (`parent_application_id`), created only by `permits.service.extend`
# (3.11b's `POST /permits/{id}/extend`) — a citizen files `POST /applications`
# itself for a `new` one, never an `extension`; see `create_draft`'s own
# docstring for why the split lives in the FUNCTION, not the wire schema.
KIND_NEW = "new"
KIND_EXTENSION = "extension"
INITIAL_STATUS = "DRAFT"
# `_own_draft_for_update`'s OTHER editable status (task 1, 3.9b): a returned
# application becomes correctable again, per that function's own docstring,
# written when it was DRAFT-only in 3.9a and already naming this. Not "task
# 3's own constant" — `submit`'s resubmission needs it too, and 3.9b has no
# second definition of what "still editable" means.
RETURNED_STATUS = "RETURNED"
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
# The `doc_types` item a benefit certificate's scan is filed under when the
# citizen attaches one. **Optional since ruling #189** (2026-09-10): the claim
# is the certificate NUMBER (ruling #181), checked against a register or by the
# leshoz (`_open_benefit_verification`); the file is supporting material the
# verifier may want to see, never a gate. Migration `0024` seeds the code —
# it is ours, not the Agency's — so the adminka can name it; nothing on the
# submission path looks it up any more. `tests/modules/applications/
# test_documents.py` holds the literal in the migration and this constant
# together.
BENEFIT_DOC_TYPE_CODE = "benefit_proof"


# A registered verifier for one benefit-category CODE — `beekeepers.service.
# match_certificate`'s own signature (`db`, then `certificate_no`/`pinfl`/
# `stir` keyword-only), returning something carrying a `.status` this module
# reads. `Callable[..., Awaitable[Any]]` rather than a `Protocol` spelling
# out the keyword-only parameters and the exact return type: pyright checks
# a `Protocol.__call__`'s return type INVARIANTLY through `Coroutine`'s
# covariant slot, so a real registration (`beekeepers.service.MatchResult`,
# a dataclass this module deliberately never imports — see below) fails
# `reportArgumentType` even though it is a plain structural match at every
# call site. `...` also sidesteps the keyword-only-vs-positional mismatch a
# precisely-typed `Callable[[AsyncSession, str, str | None, str | None],
# ...]` would have with a keyword-only signature.
#
# **Never imported here directly.** `applications` (level 3) could import a
# level-2 module's service directly (it already does, for `norms`/`gis`),
# but this seam is deliberately data-driven instead, the same idiom `norms.
# CAPACITY_LOAD_PROVIDERS`/`.EXCLUSIVITY_PROVIDERS` already use: a category
# with no registered verifier must cost this module nothing, not even an
# import, and a test proves the EMPTY seam without `beekeepers` existing in
# that test process at all.
BenefitAutoVerifier = Callable[..., Awaitable[Any]]

# Ruling #181/#182: a benefit-category CODE with no registered verifier stays
# `pending` — the leshoz's own review queue, as today. Keyed by CODE, not
# appended like `norms.LOAD_PROVIDERS`: at most one verifier owns a given
# category, and a second registration under the same code would silently
# shadow the first rather than summing two answers the way a load does.
# Starts EMPTY; `app/event_subscriptions.py` is the one place that fills it
# (`beekeeping_union_member -> beekeepers.service.match_certificate`), the
# same idiom `_register_providers` already uses for `CAPACITY_LOAD_
# PROVIDERS`/`EXCLUSIVITY_PROVIDERS`.
BENEFIT_AUTO_VERIFIERS: dict[str, BenefitAutoVerifier] = {}

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

# `admin.open_work`'s notion of "terminal" for this module (ruling R5, plan
# 07.6 task 2): a status nobody needs to act on any further. DERIVED from
# `APPLICATION_TRANSITIONS` — the same single-source idiom `archive.service`
# already uses for its own `_ELIGIBLE_APPLICATION_STATUSES` (an application
# may reach ARCHIVED only once every ACTIVE thing about it is done) — rather
# than a second, hand-typed list that could drift from the table above.
# ARCHIVED itself is added: it has no outgoing edge at all and is not a
# TARGET of anything either, so the derivation alone would miss it.
#
# Deliberately NOT narrowed to "review is over" (stopping at APPROVED): the
# guard exists to keep a departing reviewer from being untraceable, not to
# model who is doing what today, and a narrower set would need its own
# justification nothing here asks for. What it MUST do — and is proven by
# `test_a_terminal_application_does_not_block` — is let a long-serving
# reviewer with a thousand CLOSED applications still be deletable.
TERMINAL_APPLICATION_STATUSES: frozenset[str] = frozenset(
    {
        "ARCHIVED",
        *(status for status, targets in APPLICATION_TRANSITIONS.items() if "ARCHIVED" in targets),
    }
)


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
#       3.10 payments — INVOICED, PAID, EXPIRED_UNPAID, and CANCELLED from
#                       INVOICED ONLY (3.10b's withdrawal of an unpaid
#                       invoice — `tz/05` has the edge and `POST /cancel`
#                       refuses it, controller ruling R26: `payments` is the
#                       module that can take invoice-then-application in its
#                       own documented lock order, and `applications` cannot)
#       3.11 permits  — PERMIT_ISSUED, CLOSED
#       4.5 archive   — ARCHIVED (nobody's in stage 3)
#
#   SUBMITTED, IN_REVIEW, APPROVED, REJECTED, RETURNED and PENDING_INFO belong
#   to the applicant/staff flow and are branch 2's own flow verbs (`submit`,
#   `start_review`, `approve`, `reject`, `return_to_applicant`, `cancel`,
#   `forward`). Do NOT reach them through `set_status`, from any module
#   including this one — and CANCELLED only from INVOICED, as above.
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


async def public_status_lookup(db: AsyncSession, *, number: str) -> Row[Any] | None:
    """Public surface for `public.service.check_application_status` (stage 8
    fix wave finding 2): `public` may hold no import of `applications.repo`/
    `.models` (CLAUDE.md's cross-module whitelist names reports/dashboard/
    search/oversight/archive, not `public`), so this thin wrapper is the one
    door. Delegates to `repo.get_public_status_row` — see its docstring for
    exactly what the six columns are and why nothing else crosses this
    boundary. No permission or zone rule, the same as `get`/
    `current_calculation` above: the caller is another SERVICE, and the "no
    oracle" posture that makes an unknown number and a wrong phone answer
    identically is `public.service`'s own job, not this one's."""
    return await repo.get_public_status_row(db, number=number)


async def open_extension_of(
    db: AsyncSession, parent_application_id: uuid.UUID
) -> Application | None:
    """The still-open `kind='extension'` child of `parent_application_id`, or
    `None`. Thin pass-through to `repo.open_extension_of`, the fourth public
    function a level-4 caller may reach here (`get`, `current_calculation`,
    `set_status`): `permits.service.extend`'s own duplicate guard, which may
    not query `applications` directly (module boundary, CLAUDE.md) any more
    than it may write `applications.status` directly."""
    return await repo.open_extension_of(db, parent_application_id)


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

    `effective_organization` below is the public, id-taking form of this rule,
    for a level-4 caller that may not import this module's models.
    """
    if application.assigned_org_id is not None:
        return application.assigned_org_id
    if application.contour_id is None:
        return None
    return await gis_service.contour_organization(db, application.contour_id)


async def effective_organization(db: AsyncSession, application_id: uuid.UUID) -> uuid.UUID | None:
    """Which leshoz an application belongs to, by id — the public form of
    `_effective_organization` and a fourth name on this module's surface beside
    `get` / `current_calculation` / `set_status`.

    Added for `tz/12` #35 (2026-09-05): an invoice belongs to a leshoz only
    through its application, so `payments` has to ask this question to place
    one in a zone. It takes an ID and not an `Application` deliberately — a
    level-4 module may not import this module's models, so a signature naming
    that type would push every caller into breaking the boundary rule to
    satisfy a type checker.

    `None` means the application cannot be placed in any zone at all (a draft
    that names no contour and has no assignment). Every caller must treat that
    as a refusal for a zoned actor, never as "visible to everyone".
    """
    application = await repo.get_application(db, application_id)
    if application is None:
        return None
    return await _effective_organization(db, application)


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


async def _forwarded_here_by(db: AsyncSession, application: Application, *, actor: User) -> bool:
    """Ruling #107 (`tz/12` #27): whether `actor` is the one who forwarded
    THIS application up the ladder, at any level — the one case
    `_readable_application` grants READ past a zone that has since moved on.

    `_effective_organization` tracks `assigned_org_id`, and `decision._forward`
    MOVES it to the parent the moment it escalates — so the head who ran an
    over-limit case loses it from their own zone entirely the instant they
    escalate it, and cannot see how the case they ran ended, though the
    citizen still calls the office that took the filing. The decision itself
    stays with whoever it was escalated TO; this grants nothing but the read.

    The escalation is unambiguous evidence on its own: `decision._forward`
    writes an `application_status_history` row with `changed_by=actor.id` and
    `reason_text=FORWARD_REASON` for EVERY forward, at every level — so "did
    this actor ever forward this application" is exactly that row's
    existence, no separate flag and no second definition that could drift
    from `assigned_org_id`'s own history.

    Read-only, and that is exactly where this stops mattering: the WRITE
    paths (`decision._load_and_authorize`) call `_assert_in_actor_zone`
    directly and never this function, so a former forwarder who is out of
    zone still cannot approve, reject or forward what somebody else must now
    decide. A stranger head in the SAME original leshoz who never forwarded
    THIS application gets nothing here either — the check is keyed on
    `changed_by`, never on the organization.
    """
    history = await repo.list_status_history(db, application.id)
    return any(
        entry.reason_text == FORWARD_REASON and entry.changed_by == actor.id for entry in history
    )


async def status_reached_at(
    db: AsyncSession, application_id: uuid.UUID, *, status: str
) -> datetime | None:
    """When this application last entered `status`, or `None` if it never did.

    The additive accessor `permits.service`'s requisite-19 snapshot needs and
    could not have (ruling #118): the printed form's «Тўлов ҳолати ва санаси»
    wants the moment payment was confirmed, `application_status_history` is
    where that moment is recorded, and `permits` may not reach this module's
    tables any other way (module boundary, `CLAUDE.md`). Before this existed,
    that snapshot leaned on `applications.updated_at` — true today only
    because nothing else writes the row between PAID and issuance, which is a
    property of the current code rather than of the data.

    **The LAST matching row, not the first.** `PAID` is reachable once per
    invoice, but 3.9b's recalculation can send an application back through
    INVOICED, and the date the permit prints must be the payment it was
    actually issued against.

    Read-only, no zone or permission rule: this is the in-process service
    surface, the same posture `get()` documents for itself.
    """
    history = await repo.list_status_history(db, application_id)
    for entry in reversed(history):
        if entry.to_status == status:
            return entry.occurred_at
    return None


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

    **Ruling #107 carves out one more admission, checked BEFORE the zone**
    (`_forwarded_here_by`): the head who forwarded THIS application up the
    ladder keeps read access to it even after `assigned_org_id` has moved past
    their own zone. Checked ahead of `_assert_in_actor_zone` deliberately — that
    function's own refusal COMMITS an RI-12 trail before it raises (decision
    #40 ruling 2), and a forwarder who is legitimately owed this read must
    never earn a "denied" audit entry for asking.

    **Ruling #110 (`tz/12` #26): a DRAFT is unsent mail — no staff caller reads
    one they do not own, ever, zone or no zone.** Checked immediately after the
    ownership/staff gates and BEFORE both ruling #107's carve-out and the zone
    check: a DRAFT has no `assigned_org_id` (ruling 7) and cannot itself carry a
    forward, so the two checks below it can never fire for one anyway — but a
    ZONE-FREE staff caller (`Zone(None, None, None)`, e.g. `prosecutor`'s
    `view_any`) would otherwise short-circuit `_assert_in_actor_zone` and read
    every citizen's draft nationwide, the exact case «до подачи заявки в офисе
    её читать некому» exists to close. No audit entry: this is an ownership
    refusal, not a territorial one, and RI-12 stays reserved for the zone.
    """
    application = await repo.get_application(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    if application.applicant_id in await _own_applicant_ids(db, actor):
        return application
    if not await _holds_staff_read(db, actor):
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    if application.status == INITIAL_STATUS:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    if await _forwarded_here_by(db, application, actor=actor):
        return application
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
    (`effective_organization`). `action` is the caller's OWN flow-verb constant
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


async def create_draft(
    db: AsyncSession,
    payload: ApplicationCreate,
    *,
    actor: User,
    kind: str = KIND_NEW,
    parent_application_id: uuid.UUID | None = None,
) -> Application:
    """`POST /applications` — an EMPTY draft, and deliberately so (ruling 7):
    tz/04 С3 autosaves a draft field by field, so everything except who is
    filing and for whom arrives later through `PATCH`.

    The `DRAFT` row of `application_status_history` is written HERE, directly,
    and not through `set_status`: `APPLICATION_TRANSITIONS` has no edge INTO
    `DRAFT` — nothing may return an application to it — so `set_status` could
    not write this row even if asked. It is written all the same because a
    timeline that starts at `SUBMITTED` cannot say when the citizen began, and
    nothing else in the system will ever be in a position to add it.

    `kind` and `parent_application_id` are PARAMETERS and NOT fields of
    `ApplicationCreate`, on purpose (3.11b ruling 17): that model is the body
    of the public `POST /applications` and forbids nothing it does not list
    (`extra="forbid"`), so a field there is a field a citizen may set —
    `kind="extension"` against any parent id, with no permit and no holder
    behind it. Here they are supplied only by a server caller that has
    already proved both (`permits.service.extend`, 3.11a ruling 12). They
    default to today's behaviour, so `POST /applications` itself is
    unchanged, and `applications` learns nothing about permits: an extension
    is an application shape `APPLICATION_KINDS` has carried since migration
    0015.
    """
    if kind not in APPLICATION_KINDS:
        # Before `flush()`: the `kind_valid` CHECK would otherwise surface a
        # caller's typo as an `IntegrityError` — no handler maps it, so it
        # would reach the client as a 500 (lesson: walk every caller-settable
        # field that is an FK or an enum-ish column before `flush()`).
        raise err("ERR-VAL-001", details={"reason": "unknown_kind"})
    applicant_id, representation_id = await _resolve_applicant(db, payload, actor=actor)
    application = Application(
        applicant_id=applicant_id,
        submitted_by_user_id=actor.id,
        on_behalf=payload.on_behalf,
        representation_id=representation_id,
        status=INITIAL_STATUS,
        channel=CHANNEL_PORTAL,
        kind=kind,
        parent_application_id=parent_application_id,
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


# DELIBERATELY both, not `DRAFT` alone — do NOT narrow this back (task 1
# review finding): PATCH, the document routes and `submit`'s own
# resubmission all share this one set, because an application returned for
# correction (3.9b) exists so the applicant CAN correct it — a return
# nobody can act on would make the whole feature pointless. Covered by
# `tests/modules/applications/test_draft_api.py::
# test_the_owner_may_patch_a_returned_application` and
# `test_documents.py::test_the_owner_may_attach_and_detach_on_a_returned_
# application`, each with a stranger-is-still-refused sibling.
_EDITABLE_STATUSES = frozenset({INITIAL_STATUS, RETURNED_STATUS})


async def _own_draft_for_update(
    db: AsyncSession, application_id: uuid.UUID, *, actor: User
) -> Application:
    """The caller's own application, locked, and only while it is still
    EDITABLE — `DRAFT`, or `RETURNED` (task 1, 3.9b): a returned application
    becomes correctable again, and PATCH/documents/`submit` must reach it
    exactly as they reach DRAFT (see `_EDITABLE_STATUSES`'s own comment for
    why this is deliberate and covered, not an oversight to "fix" back to
    DRAFT-only).

    Ownership is checked BEFORE the status, and both refusals differ: a
    stranger gets 404 (they may not learn that this id is an application at
    all), while the owner of an application that has moved on gets 409
    `ERR-APP-004` — a conflict with the application's current state, which is
    the honest answer to "why can I no longer edit this".

    Locked (`repo.get_application_for_update`) because this is a read-check-
    write over `status`: without it a PATCH and a concurrent `submit` both read
    the same editable status, both pass, and the edit lands on an application
    that is already submitted — its signed package then describes something
    the stored row no longer says.
    """
    application = await _own_application_for_update(db, application_id, actor=actor)
    if application.status not in _EDITABLE_STATUSES:
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

    **Changing the contour CLEARS the frozen pair** (final review). Ruling 19
    leaves `contour_version_id` and `requested_area_ha` committed on a draft
    whose submission was refused after step 4 — deliberately: they are evidence
    of a genuine attempt. But a draft that then moves to a different contour
    would carry a version belonging to the OLD one, and a pair that names two
    different plots is never right, however little reads it today. Clearing
    both here keeps "the frozen version belongs to the frozen contour" true at
    every moment rather than only at the moments something happens to look.
    """
    application = await _own_draft_for_update(db, application_id, actor=actor)
    fields = patch.model_dump(exclude_unset=True)
    await _assert_references(db, fields)
    # Read BEFORE the replacement: `repo.replace_items` deletes the old rows,
    # so afterwards there is nothing left to snapshot them from.
    before = _snapshot(application, await repo.list_items(db, application.id))
    items = fields.pop("items", None)
    if "contour_id" in fields and fields["contour_id"] != application.contour_id:
        # Not audited separately: the two columns are DERIVED (step 4 writes
        # them from the contour version), never client-supplied, so `_snapshot`
        # does not carry them and the `contour_id` change the entry does record
        # is the whole of what the applicant did.
        application.contour_version_id = None
        application.requested_area_ha = None
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
    checks, its conclusions and its current price.

    `checks` is the FULL list, never the latest per type: a repeat check is a
    new row and the history is the evidence (ruling 12); `conclusions` (task 5)
    is the same shape for the identical reason (ruling 10). `calculation` is
    `current_calculation` — the newest `calculations` row, which is what
    `payments` invoices from — read through this module's own public surface
    rather than by querying `norms`' tables, so the card and the invoice can
    never disagree about which calculation is current.

    Both keys are present and empty/null from task 3, before anything can write
    either: they are a contract 3.10a and 3.11a already read
    (`card["calculation"]["amount"]`), not a placeholder a later task adds.

    `sla_overdue` (task 4, 3.9b, ruling 8) is computed HERE, not on the schema:
    `sla.is_overdue` needs both `status` (an OPEN pause suspends the clock
    whatever the stored deadline says) and the wall clock. A `DRAFT` or a
    decided/terminal application has no deadline at all, and `False` is the
    honest answer for it — never overdue, having never been timed.
    """
    application = await _readable_application(db, application_id, actor=actor)
    deadline = application.sla_deadline_at
    return {
        "application": application,
        "items": await repo.list_items(db, application.id),
        "documents": await repo.list_documents(db, application.id),
        "checks": await repo.list_checks(db, application.id),
        "conclusions": await repo.list_conclusions(db, application.id),
        "calculation": await current_calculation(db, application.id),
        "sla_overdue": (
            False
            if deadline is None
            else sla.is_overdue(application.status, deadline, datetime.now(UTC))
        ),
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
    still unassigned (`repo._zone_join_target`, `effective_organization`).

    The contour half of that join is `gis.service.contour_organization_column`,
    called HERE and handed to the repo as an expression: cross-module calls
    live in the service layer, and a repo calling another module's service
    inverts the layering even where the boundary rule itself is satisfied
    (review I2).

    **Ruling #110 excludes `INITIAL_STATUS` from the STAFF half only** —
    otherwise this function's own "can never disagree with the card" promise
    above would be broken by the very ruling that promise is supposed to
    survive: `_readable_application` now 404s a staff caller on a DRAFT it
    does not own, and a list that still named that DRAFT would be LEAKING
    through the one door the card just closed. The owner's own scope
    (`holder_ids`) is untouched — they see every status of their own,
    DRAFT included, throughout.
    """
    scope: list[Any] = []
    holder_ids = await _own_applicant_ids(db, actor)
    if holder_ids:
        scope.append(Application.applicant_id.in_(holder_ids))
    if await _holds_staff_read(db, actor):
        scope.append(
            and_(
                Application.status != INITIAL_STATUS,
                zone_filter(
                    zone_of(actor),
                    region_col=Organization.region_id,
                    district_col=Organization.district_id,
                    organization_col=Organization.id,
                ),
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
# Ruling 18 (в), the second half (2026-09-05): `signatures.service.sign()`'s
# `content_changed_reason` opt-in, passed at both places this module signs a
# freshly re-priced package (`submit` below and `decision._sign_decision`) —
# never at any OTHER `sign()` call in the codebase, which is exactly why this
# constant lives here and not in `signatures`. See `_package_bytes`' and
# `package`'s own docstrings for what "the package" is and why it can drift.
STALE_PACKAGE_REASON = "package_changed"
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

    **Byte-identical to `schemas._trim_decimal` today, and the two MUST NOT be
    merged** (final review, and the same warning `checks._jsonable` carries
    against `_json_safe`). That one renders a `Decimal` for an API RESPONSE and
    may be changed whenever a client needs a different presentation; this one
    renders it into bytes that have already been SIGNED, and every existing
    signature stops verifying the day its output moves. They are the same
    function only by coincidence of both being right today — one shared helper
    would couple a frozen non-repudiation format to a presentation decision, so
    the duplication IS the boundary.
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

    **RULING 23 — a stale package is an accepted exposure, and this is the
    function it starts in.** The amount comes from `norms.service.preview`,
    which prices at `business_today()` against whatever tariffs and БҲМ are
    effective right then. So a tariff or `rule_parameter` published between the
    `GET /package` and the `POST /submit`, a norm published or archived, or
    plain midnight in Tashkent, changes these bytes. **Ruling 18 (в),
    2026-09-05: accepted permanently — do NOT "fix" it here by caching the
    package or by dropping the amount from it** — but no longer left
    unexplained: `submit` and `decision._sign_decision` both pass
    `STALE_PACKAGE_REASON` into `signatures.service.sign()`, so a signer whose
    package genuinely moved out from under them meets
    `details.reason == "package_changed"` rather than a bare
    `"signature_invalid"` indistinguishable from a forged one.

    **`contour_version_id` is the version these bytes NAME, and which version
    that is depends on whether the application has frozen one yet.** `package`
    resolves it and passes it here: `gis.service.published_version` while the
    application is a DRAFT (step 4 has not run, the column is still null or
    stale), and `application.contour_version_id` itself from SUBMITTED onward.
    The argument therefore wins over the column only where the column is not
    yet the answer — see `package`'s own docstring for why the frozen column
    must win afterwards, and what breaks when it does not.
    """
    version_id = contour_version_id or application.contour_version_id
    if version_id is None:
        # Never an `assert` on a request path — `-O` strips it, and what this
        # function returns is SIGNED (review round 2, minor 9). Unreachable
        # today, but for a different reason than "both callers pass one
        # in": both call sites now GUARANTEE a non-null id before calling
        # here — `package` by falling back to `_published_version_or_refuse`
        # when the frozen column is null (a cancelled-from-DRAFT application),
        # `submit` by freezing `contour_version_id` in the same transaction
        # that sets the status.
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


async def _assert_complete(
    db: AsyncSession, application: Application, *, rules_accepted: bool = True
) -> None:
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

    **`rules_accepted` (ruling #184) IS a missing field, folded into the SAME
    list rather than a separate check** — `package` (the other caller, whose
    `GET` carries no body and therefore no opinion on the checkbox) passes
    nothing and gets the default `True`, so only `submit` can ever name
    `rules_accepted` here.
    """
    missing = await checks.missing_for_pricing(db, application)
    if not rules_accepted:
        missing.append("rules_accepted")
    if missing:
        raise err("ERR-APP-001", details={"missing": missing})


async def _open_benefit_verification(db: AsyncSession, application: Application) -> None:
    """Step 3b. Ruling #181: EVERY benefit category now needs a certificate
    number — refused for every category with none, not only the ones a
    per-item switch used to flag. **`requires_certificate` is no longer read
    at all; the branch reading it is DELETED, not merely skipped** — the
    number is universal, so there is nothing left for that property to gate.

    **The seam, ruling #182.** A category with a registered
    `BENEFIT_AUTO_VERIFIERS` entry (keyed by the classifier item's own
    `code`) is checked automatically against whatever register that verifier
    speaks for — today, `beekeeping_union_member` against the Beekeeping
    Union's own register, wired from `app/event_subscriptions.py`, never
    imported here directly (see `BenefitAutoVerifier`'s own docstring for
    why). `matched` verifies the claim ON THE SPOT — `benefit_verified_by =
    NULL` means "the register", ruling #182's own words, distinct from a
    human verifier's real id. `unknown`/`not_yours` refuse the SUBMISSION
    itself (422 `ERR-APP-003`): a number that is not provably the applicant's
    own is not evidence, and letting the filing through `pending` would ask
    the leshoz to re-decide what the register already answered. A category
    with NO registered verifier — every #181 recreation category today —
    stays `pending`, exactly as before: the leshoz's own review queue.

    **Identity for the auto-verifier is read off the SAME `Applicant` row
    `submit`'s other steps already use** (`auth_service.get_applicant`), not
    branched on `on_behalf`: an `individual` applicant (`on_behalf="self"`)
    carries `pinfl` and no `stir`, a `legal` one (`on_behalf="legal"`) the
    reverse (`identity_by_kind`, `auth/models.py`), so passing both straight
    through and letting the verifier's own "PINFL when given, else STIR" rule
    pick is the SAME split ruling #182 asks for, with no second conditional
    to drift from the CHECK that already enforces it.

    Integration finding, stage 9 wave 2 — and the exact shape this project's
    defects keep taking. T9 added the five columns and the verifier's whole
    workplace, T6 made issuance refuse a `pending` claim, and both were green:
    nothing ever SET `pending`. Every benefit claim would have sailed past the
    office built to check it, with the certificate number never asked for, and
    the only visible symptom would have been a verifier's empty list — which
    reads exactly like a quiet week.

    Fail-closed on the unconfigurable case: a claim whose classifier item
    cannot be read is refused, not waved through as `not_required`. The
    certificate's scan (`BENEFIT_DOC_TYPE_CODE`) is NOT required here —
    ruling #189: the number is the claim, the file is optional support.
    """
    item_id = application.benefit_category_item_id
    if item_id is None:
        application.benefit_verification_status = "not_required"
        return
    item = await admin_repo.get_classifier_item(db, item_id)
    if item is None:
        raise err("ERR-APP-003", details={"reason": "unknown_benefit_category"})
    if not (application.benefit_certificate_no or "").strip():
        raise err("ERR-APP-003", details={"reason": "benefit_certificate_required"})

    verifier = BENEFIT_AUTO_VERIFIERS.get(item.code)
    if verifier is None:
        # A RESUBMISSION must not silently keep a verdict made about the
        # previous attempt: the applicant may have changed the number since
        # it was rejected.
        application.benefit_verification_status = "pending"
        application.benefit_verified_by = None
        application.benefit_verified_at = None
        application.benefit_rejection_reason = None
        return

    applicant = await auth_service.get_applicant(db, application.applicant_id)
    result = await verifier(
        db,
        certificate_no=(application.benefit_certificate_no or "").strip(),
        pinfl=applicant.pinfl if applicant is not None else None,
        stir=applicant.stir if applicant is not None else None,
    )
    if result.status == "matched":
        application.benefit_verification_status = "verified"
        application.benefit_verified_by = None
        application.benefit_verified_at = datetime.now(UTC)
        application.benefit_rejection_reason = None
    elif result.status == "unknown":
        raise err("ERR-APP-003", details={"reason": "benefit_certificate_unknown"})
    else:  # "not_yours"
        raise err("ERR-APP-003", details={"reason": "benefit_certificate_not_yours"})


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

    **No status rule of its own, and the version it names is what makes that
    safe.** The route serves a DRAFT (the applicant is about to sign it) and an
    application under review (the head signs the very same bytes — controller
    ruling R5, `decision._sign_decision` fetches them through this function), so
    a status restriction would break the decision path. What the two cases do
    NOT share is which contour version the bytes name: see the comment below.

    **RULING 23 applies here, in full.** This route prices through
    `norms.service.preview` at `business_today()`, exactly as `submit` does a
    moment later, and NOTHING freezes the answer in between. A tariff or
    `rule_parameter` published between the two calls, a norm published or
    archived, or midnight in Tashkent, changes the bytes — and the signer
    signs one package while the server verifies against another. **Accepted
    permanently by ruling 18 (в), 2026-09-05** — no cache, no freeze, no
    dropping the amount — but the two callers that sign what this route just
    served (`submit` and `decision._sign_decision`) both pass
    `content_changed_reason=STALE_PACKAGE_REASON` into `sign()`, so a signer
    who did nothing wrong meets `details.reason == "package_changed"` rather
    than the bare, indistinguishable-from-forgery `"signature_invalid"`. Do
    not cache the package or drop the amount from it here — that is exactly
    what was decided against.
    """
    application = await _readable_application(db, application_id, actor=actor)
    await _assert_complete(db, application)
    # **The FROZEN version wins the moment there is one.** A DRAFT has not
    # reached step 4 yet, so the only honest answer is the version currently
    # published — and `_published_version_or_refuse` is also the 409 for a
    # contour whose geometry is still a draft. From SUBMITTED onward the
    # application is BOUND to the version step 4 froze (`permits.service.issue`
    # reads that same column), and `gis` allows one published version per
    # contour which may be superseded at any time: pricing the CURRENT one here
    # would make the head's decision signature attest to version B while the
    # application, and the permit printed from it, name version A. A verifier
    # re-deriving these bytes from the stored row would then get a different
    # string and the signature would not verify — pinned by
    # `test_decision.py::test_a_republished_contour_does_not_invalidate_the_
    # decision_signature`.
    # **The DRAFT branch stays status-keyed, not null-keyed.** Ruling 19
    # leaves a STALE frozen version on a refused submission, so a draft must
    # always re-resolve the current published version, even when its column
    # happens to be set from an earlier attempt — `is None` here would serve
    # the stale one instead.
    frozen = None if application.status == INITIAL_STATUS else application.contour_version_id
    # The fallback below can never fire for a SUBMITTED-or-later application:
    # `submit` freezes `contour_version_id` in the same transaction that sets
    # the status. The only status that reaches it is CANCELLED-from-DRAFT — a
    # draft completed and then withdrawn without ever being submitted, so the
    # column was never frozen — and there `_published_version_or_refuse` gives
    # the honest 409 for a contour whose geometry has since gone back to draft,
    # rather than `_package_bytes` finding a null and answering ERR-SYS-001 for
    # an application that is perfectly able to show what it once priced.
    version_id = frozen or (await _published_version_or_refuse(db, application)).id
    _, priced = await _price(db, application, actor=actor)
    return _package_bytes(application, priced, contour_version_id=version_id)


async def submit(
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    pkcs7: str | None,
    rules_accepted: bool,
    actor: User,
    ip: str | None = None,
) -> Application:
    """`POST /applications/{id}/submit` — the fourteen steps of the block
    comment above, in one transaction.

    **RULING 23, restated at the third of its three required places.** The
    package is priced afresh HERE, and the bytes the client signed came from a
    separate `GET /package` call priced at its own moment. A tariff, a
    `rule_parameter`, a norm or the Tashkent date moving in between makes the
    two disagree and the applicant's signature no longer verifies against what
    `submit` just recomputed.

    **RULING 18 (в), 2026-09-05: the exposure itself is accepted, permanently —
    no freeze table, no TTL, no migration — but `sign()` is told about it.**
    Passing `content_changed_reason=STALE_PACKAGE_REASON` does not stop this
    from happening; it only tells the difference apart in what gets raised.
    `signatures.service.sign()` already has both halves of that comparison in
    hand — the bytes it just hashed and the hash the envelope actually
    signed — so when they disagree under an otherwise-valid signature, the
    422 carries `details.reason == "package_changed"` instead of the bare
    `"signature_invalid"` a genuinely broken or forged signature still gets.
    The applicant meets "the price changed while you were signing, please
    re-open the form" instead of a cryptographic error for something they did
    not do; `test_submit.py`'s
    `test_a_price_that_moved_after_signing_is_labeled_package_changed` pins it,
    and the paired `test_an_invalid_signature_refuses_the_submission_whole`
    pins the negative — a genuinely bad signature keeps the generic reason.

    **Ruling #184, `rules_accepted`.** Folded into step 2's completeness list
    (`_assert_complete`), never a check of its own — `false` is 400
    `ERR-APP-001` naming `rules_accepted` alongside whatever else is missing.

    **Ruling #183, the signature at step 8.** `pkcs7` present signs exactly as
    before, whatever `on_behalf` says (a legal entity, or a citizen who still
    has and used a certificate, is untouched). `pkcs7` absent: `on_behalf ==
    "self"` signs with the button (`sign_simple`, over the SAME bytes `sign()`
    would otherwise verify — the package's shape never changes for this); a
    legal entity with no envelope is refused, 422 `ERR-SIGN-001`
    `simple_signature_not_allowed` — decision #9 still requires ERI of a
    representative, and ruling #183 narrows that only for `self`.
    """
    # Step 0. Minted before anything is written, because it is what step 8
    # signs and what step 11 stores as the history row's primary key (ruling
    # 25) — and minting it first is what lets a signature exist for a REFUSED
    # attempt whose history row is never written. That orphan is the evidence
    # trail working, not a leak: `sign()` commits its refusal, nothing else is
    # written, and the row says "an attempt was made against submission X and
    # it was rejected".
    submission_id = uuid7()
    # Step 1. The owner's own DRAFT (or, task 1, 3.9b: RETURNED), locked: 404
    # for a stranger, 409 `ERR-APP-004` for an application that has moved on
    # some other way. Captured before anything overwrites `application.status`
    # below — a RESUBMISSION's history row and audit entry must say
    # `from_status="RETURNED"`, not a hardcoded "DRAFT" that was true only for
    # the FIRST submission.
    application = await _own_draft_for_update(db, application_id, actor=actor)
    from_status = application.status
    # Ruling #183, decided FIRST (stage 10 review, finding 9): a legal entity
    # with no envelope can never succeed, and that is known from the body and
    # `on_behalf` alone — refusing here spends nothing on steps 2-7 and leaves
    # no half-attempt to commit. Nothing has been written yet, so a plain
    # raise is the whole refusal; the audit row a refused ATTEMPT earns
    # belongs to attempts that got as far as the package.
    if pkcs7 is None and application.on_behalf != "self":
        raise err("ERR-SIGN-001", details={"reason": "simple_signature_not_allowed"})
    await _assert_complete(db, application, rules_accepted=rules_accepted)  # step 2
    # Ruling #184: stamped from the SERVER clock, not the client's claim —
    # `_assert_complete` above already refused a `false` value, so reaching
    # here means the box was checked. Overwritten on every attempt, including
    # a RESUBMISSION after RETURNED (each one requires the checkbox again),
    # never merely left from an earlier try.
    application.rules_accepted_at = datetime.now(UTC)
    # Step 3 used to demand a `benefit_proof` attachment for every claim;
    # ruling #189 dropped it — the number below is the claim, the scan is
    # optional support for the verifier.
    await _open_benefit_verification(db, application)  # step 3b (rulings #181/#182)
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
    #
    # Ruling #183: `pkcs7` present signs exactly as before, whatever
    # `on_behalf` says. Absent: `self` signs with the button — this module's
    # OWN rule for when a simple signature may stand in for the filing's
    # signature, the same way `permits.service.add_signature` decides it for
    # the holder's line on a permit (read, not copied — the filing has one
    # signer and one purpose, never four). `legal` with no envelope was
    # refused at step 1; the `else` below is the belt for the same rule,
    # kept evidence-then-raise like every refusal that got this far.
    document = _package_bytes(application, priced, contour_version_id=version.id)
    if pkcs7 is not None:
        await signatures_service.sign(
            db,
            object_type=SUBMISSION_OBJECT_TYPE,
            object_id=submission_id,
            purpose=SUBMISSION_PURPOSE,
            document=document,
            pkcs7=pkcs7,
            user=actor,
            content_changed_reason=STALE_PACKAGE_REASON,
            ip=ip,
        )
    elif application.on_behalf == "self":
        await signatures_service.sign_simple(
            db,
            object_type=SUBMISSION_OBJECT_TYPE,
            object_id=submission_id,
            purpose=SUBMISSION_PURPOSE,
            document=document,
            user=actor,
            ip=ip,
        )
    else:
        await audit.log(
            db,
            action=APPLICATION_SUBMIT,
            user_id=actor.id,
            object_type="application",
            object_id=application.id,
            result="denied",
            basis="simple_signature_not_allowed",
        )
        await db.commit()
        raise err("ERR-SIGN-001", details={"reason": "simple_signature_not_allowed"})

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
    # Step 10, ruling 5а — conditional since ruling 16.1 (3.9b task 3): a
    # RESUBMISSION after RETURNED keeps its number rather than allocating a
    # second one, which would break the per-year counter design/03 requires
    # to stay continuous. `application.number` is set only once, on the FIRST
    # submission (below, inside the savepoint) — a non-null value here means
    # this is not that first attempt.
    number = application.number
    if number is None:
        # AFTER the signature, so a refused ERI never reaches the counter at
        # all; inside the transaction, so a failure later than this rolls the
        # counter back with it and the year's numbering has no holes.
        number = await next_public_number(db, NUMBER_PREFIX, business_today())

    # Ruling 16.1's other half: `submitted_at`/`sla_deadline_at` belong to the
    # FIRST submission alone — task 2's SLA clock must not reset on a
    # resubmission, so both are read off the row first and only computed when
    # still unset.
    submitted_at = application.submitted_at
    sla_deadline_at = application.sla_deadline_at
    if submitted_at is None:
        submitted_at = datetime.now(UTC)
        sla_deadline_at = submitted_at + timedelta(days=SLA_DAYS)
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
            application.sla_deadline_at = sla_deadline_at
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
            from_status=from_status,
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
        old_value={"status": from_status},
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
    # Task 1 (3.9b), and genuinely the LAST step: an application must never
    # exist in SUBMITTED with no assignment row at all (ruling 7). See the
    # hook's own docstring for the resubmission guard (ruling 6).
    await _auto_assign_on_submission(db, application)
    # The hook may have just written `assigned_org_id`/`assigned_user_id`
    # (a plain UPDATE, whose `onupdate=func.now()` on `updated_at` is not
    # reloaded automatically — lesson: "the row in memory is not what
    # Postgres stored"). Refresh unconditionally rather than branching on
    # whether it actually wrote anything.
    await db.refresh(application)
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
# Controller ruling R26: the statuses `POST /cancel` accepts as a SOURCE — a
# narrower set than `APPLICATION_TRANSITIONS[...]` contains CANCELLED in, and
# deliberately so. See `cancel`'s docstring for why the table keeps
# `INVOICED -> CANCELLED` that this route refuses, and who drives it instead.
CANCELLABLE_BY_APPLICANT_STATUSES = frozenset({INITIAL_STATUS, SUBMITTED_STATUS, IN_REVIEW_STATUS})

# Ruling 25's OTHER half, beside `SUBMISSION_OBJECT_TYPE`/`SUBMISSION_PURPOSE`:
# a DECISION is signed as `("application", <the application id>,
# "application_decision")` — one such object per application, however many
# submission attempts it took. Declared here because the TIMELINE reads them
# today; task 7 signs with them.
DECISION_OBJECT_TYPE = "application"
DECISION_PURPOSE = "application_decision"

# `application_assignments.reason` (`models.ASSIGNMENT_REASONS`). 3.9a had no
# auto-assignment job — ruling 14 let any reviewer in the zone pick an
# application up — so every row that stage wrote recorded a human act.
# `ASSIGNMENT_AUTO` is 3.9b task 1's own: the reason on the row
# `_auto_assign_on_submission` writes, for a reviewer picked FOR them rather
# than BY them.
ASSIGNMENT_MANUAL = "manual"
ASSIGNMENT_AUTO = "auto"


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
    fields_to_fix: dict[str, Any] | None = None,
) -> ApplicationStatusHistory:
    """Move an ALREADY-LOCKED application one legal edge, and leave the two
    records every transition owes behind: the `application_status_history` row
    and one `audit_log` entry under the CALLER'S flow verb (ruling 17).

    `reason_item_id`/`legal_basis` are task 7's rejection grounds (`tz/04` С8);
    `fields_to_fix` is task 3's own, beside them (3.9b) — a JSON OBJECT, never
    a list (`models.py`'s column is `Mapped[dict[str, Any] | None]`). All three
    are set HERE, before the insert, never on the returned row: migration
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
        fields_to_fix=fields_to_fix,
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
    if fields_to_fix is not None:
        new_value["fields_to_fix"] = fields_to_fix
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
    actor: User | None,
) -> ApplicationAssignment:
    """CLAIM the active assignment if it is unheld or already names this same
    person, otherwise SUPERSEDE it — the ONE write path into
    `application_assignments` (ruling 16.2).

    Three cases, checked in this order:

    * an active row exists and its `user_id` is `NULL`, or already equals the
      `user_id` being set here → **claim**: only `user_id` is written: the
      row, its `id` and its OWN `reason` are left exactly as they were. The
      "already equals" half is not in the ruling's own text but is load-
      bearing on the EXISTING 3.9a suite, not just this task's: once
      auto-assignment can pick a real candidate, that candidate is very often
      the SAME person who then calls `start-review` on their own file, naming
      `user_id=actor.id` — `test_a_hodim_in_the_zone_takes_it_into_work` and
      `test_claiming_an_assignment_twice_supersedes_instead_of_colliding`
      both pin `len(timeline["assignments"])` on that reclaim NOT superseding.
    * an active row exists and names somebody else (a real, DIFFERENT
      `user_id`) → **supersede**: deactivate it, `flush()`, then insert the
      new row. `uq_application_assignments_active` is UNIQUE on
      `(application_id) WHERE is_active`, so a blind second insert beside a
      live row is an `IntegrityError`, and the flush between the two is NOT
      optional — without it both rows are pending when the index is checked
      and the insert fails on a conflict the flush would have resolved
      (lesson: "A partial unique index constrains only the rows it covers,
      and only after a flush").
    * no active row at all → insert.

    Three callers, one helper: `_auto_assign_on_submission` (`reason='auto'`,
    `actor=None` — nobody's personal act, so `assigned_by` stays `NULL`),
    `start_review` (`reason='manual'`, claiming the auto row), and `assign`
    (`POST /assign`, a caller-supplied reason, usually superseding it).
    """
    active = await repo.get_active_assignment(db, application.id)
    if active is not None and (active.user_id is None or active.user_id == user_id):
        active.user_id = user_id
        await db.flush()
        await db.refresh(active)
        return active
    await repo.deactivate_assignments(db, application.id)
    await db.flush()
    row = ApplicationAssignment(
        application_id=application.id,
        org_id=org_id,
        user_id=user_id,
        assigned_by=None if actor is None else actor.id,
        reason=reason,
        is_active=True,
    )
    await repo.add_assignment(db, row)
    # `created_at` is a `server_default` the INSERT leaves unloaded, and the
    # timeline both sorts on it and serializes it.
    await db.refresh(row)
    return row


async def _auto_assign_on_submission(db: AsyncSession, application: Application) -> None:
    """`submit`'s last step (task 1): give the freshly SUBMITTED application a
    reviewer, or failing that, at least an organization — ruling 7, "the
    application is still assigned to the ORGANIZATION [...] it must never
    silently fail to assign".

    **Guarded to a FIRST submission on an UNCHANGED contour** (ruling 6, and
    the final whole-branch review's Critical). `submit` also drives a
    RESUBMISSION after 3.9b's return for correction, and firing this hook
    unconditionally would run `choose_executor` again — the supersede branch
    of `_claim_assignment` would then silently hand the file to a fresh
    auto-pick, taking it away from the very reviewer who returned it. An
    application that already carries an active `application_assignments` row
    is therefore left untouched, **unless the contour it now names is owned
    by a DIFFERENT leshoz than the one the row was assigned to.**

    That second case is not hypothetical: RJ-01 plus `fields_to_fix:
    {contour_id}` is "wrong plot, pick the right one", and correcting a plot
    can legitimately move it to another leshoz. `assigned_org_id`, once
    written, is a plain stored column — `_effective_organization` returns it
    verbatim without re-checking the contour — so without this branch a
    corrected application would resubmit into SUBMITTED still bearing the
    FIRST leshoz's `assigned_org_id` while naming the SECOND leshoz's plot:
    the first leshoz keeps reviewing and signing a permit for land it does
    not own, and the second leshoz cannot even see its own application. The
    fix re-derives the organization straight from `gis.service` (never
    through `_effective_organization`, which would just echo the stale value
    back) and, only when it disagrees with the stored one, drops the stale
    assignment and falls through to the same fresh pick a first submission
    gets. Refusing the resubmission instead was considered and rejected: RJ-01
    exists precisely so the office can say "pick the right plot", and a typed
    error here would make that instruction unusable.
    """
    active = await repo.get_active_assignment(db, application.id)
    organization_id: uuid.UUID | None
    if active is not None:
        # Re-derived straight from `gis.service`, never through
        # `_effective_organization` — that helper returns
        # `application.assigned_org_id` VERBATIM whenever it is set, which is
        # exactly the stale value this branch exists to catch, not confirm.
        # `contour_id is None` is unreachable from a genuinely SUBMITTED
        # application (`_assert_complete`'s own guard, `_effective_
        # organization`'s identical null check below) but is treated as "no
        # change" rather than narrowing the type with an assert, the same
        # fail-closed shape this function already uses for `organization_id`.
        current_org = (
            None
            if application.contour_id is None
            else await gis_service.contour_organization(db, application.contour_id)
        )
        if current_org is None or current_org == application.assigned_org_id:
            return
        # The contour's owner changed under an existing assignment: supersede
        # it explicitly (a plain deactivate, not `_claim_assignment`'s own
        # supersede branch) so the fresh insert below always carries the NEW
        # `org_id` — `_claim_assignment`'s "claim" branch writes `user_id`
        # alone and would leave the row's `org_id` stale on the one coincidence
        # where the new pick's `user_id` matches the old one's (both `None`,
        # say, if neither leshoz has an eligible reviewer).
        await repo.deactivate_assignments(db, application.id)
        organization_id = current_org
    else:
        organization_id = await _effective_organization(db, application)
    if organization_id is None:
        # Unreachable from a genuinely SUBMITTED application —
        # `_assert_complete` makes `contour_id` mandatory before `submit`
        # ever gets here — but this step fails closed rather than raising
        # mid-submission for work that is not itself what the applicant is
        # waiting on.
        return
    eligible = await auth_service.user_ids_with_permission(
        db, APPLICATIONS_REVIEW, organization_id=organization_id
    )
    picked = choose_executor(await repo.review_candidates(db, eligible))
    application.assigned_org_id = organization_id
    application.assigned_user_id = picked
    await _claim_assignment(
        db,
        application,
        org_id=organization_id,
        user_id=picked,
        reason=ASSIGNMENT_AUTO,
        actor=None,
    )


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


APPLICATION_ASSIGN = "application.assign"


async def assign(
    db: AsyncSession, application_id: uuid.UUID, *, user_id: uuid.UUID, reason: str, actor: User
) -> Application:
    """`POST /applications/{id}/assign` — sys_admin names who holds an
    application, superseding whatever assignment it has now.

    **Gated entirely at the route.** `require_permission(APPLICATIONS_ASSIGN)`
    is `sys_admin`-only (migration 0015's `ROLE_GRANTS`; Task 1 ANSWERED (б),
    2026-09-05 — an `executor_head` gets 403 `ERR-ACL-001` from the dependency,
    never a refusal from here), so there is no zone check in this function
    either: the one role that can reach it at all is already unrestricted
    nationwide (decision #41 ruling 2), the same reasoning `_forward` in
    `decision.py` states for an unzoned agency-level head.

    Reassignment goes through `_claim_assignment` (ruling 16.2) — the SAME
    helper `start_review` and the auto-assignment hook use: it supersedes an
    active row naming somebody else, or claims one that is unheld or already
    names this exact person.
    """
    application = await repo.get_application_for_update(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    organization_id = await _effective_organization(db, application)
    if organization_id is None:
        # Same unreachable-but-named-anyway guard as `start_review`'s own:
        # `application_assignments.org_id` is NOT NULL and a DRAFT with no
        # contour yet has nothing to put in it.
        raise err(
            "ERR-APP-004",
            details={"reason": "no_organization", "status": application.status},
        )
    # Set BEFORE `_claim_assignment`, not after — that call's own flush is what
    # carries these two along with it, the same order `start_review` uses;
    # `db.refresh` right after relies on nothing here being separately dirty.
    application.assigned_org_id = organization_id
    application.assigned_user_id = user_id
    await _claim_assignment(
        db, application, org_id=organization_id, user_id=user_id, reason=reason, actor=actor
    )
    await db.refresh(application)
    await audit.log(
        db,
        action=APPLICATION_ASSIGN,
        user_id=actor.id,
        object_type="application",
        object_id=application.id,
        old_value={},
        new_value={"assigned_org_id": str(organization_id), "assigned_user_id": str(user_id)},
        basis=reason,
    )
    return application


# --- Task 3 (3.9b): return for correction --------------------------------

APPLICATION_RETURN = "application.return"
# `notification_templates.event_code` — DOTTED, seeded by migration 0025
# (task 2's own). No bus event beside it: unlike submit/approve/reject/cancel,
# nothing above this module subscribes to a return — it is a same-level
# bounce back to the applicant, not a signal `payments`/`permits` act on.
NOTIFY_APPLICATION_RETURNED = "application.returned"
# The SAME `rejection_reasons` classifier `decision._reason_item` reads
# (tz/10 §8.2) — repeated here rather than imported because `decision.py`
# imports `service.py`, never the reverse. Ruling 3's KIND check below is this
# function's own; `decision.reject` checks no kind at all.
RETURN_REASON_CLASSIFIER_CODE = "rejection_reasons"
# `classifier_items.props["kind"]` values a RETURN may cite (0005_admin_seeds;
# 0025 recast RJ-15 from "reject" to "both"). RJ-03 ("plot outside the forest
# fund") types "reject" and is refused with `reason_not_returnable` —
# returning under it would misdescribe the decision and hand the applicant
# something they cannot fix.
RETURNABLE_REASON_KINDS = frozenset({"return", "both"})
# "field names that exist on the application" (task 3's own words): the
# columns of `applications` itself. An existence check, not a validity check
# (lesson) — naming `created_at` passes membership exactly as a nonsensical
# but real classifier item id passes `_assert_references`.
_APPLICATION_FIELD_NAMES = frozenset(column.name for column in Application.__table__.columns)


async def _return_reason_item(db: AsyncSession, reason_item_id: uuid.UUID) -> ClassifierItem:
    """The RJ-* ground a return is made on. 422 `unknown_rejection_reason` for
    an id outside the ACTIVE `rejection_reasons` classifier (same membership
    check as `decision._reason_item`); 422 `reason_not_returnable` for one
    that IS in it but types a refusal or a withdrawal instead (ruling 3)."""
    item = await admin_repo.get_classifier_item(db, reason_item_id)
    classifier = await admin_repo.get_classifier_by_code(db, RETURN_REASON_CLASSIFIER_CODE)
    if (
        item is None
        or classifier is None
        or item.classifier_id != classifier.id
        or item.status != "active"
    ):
        raise err("ERR-VAL-001", details={"reason": "unknown_rejection_reason"})
    if item.props.get("kind") not in RETURNABLE_REASON_KINDS:
        raise err("ERR-VAL-001", details={"reason": "reason_not_returnable"})
    return item


async def return_to_applicant(
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    reason_item_id: uuid.UUID,
    fields_to_fix: dict[str, Any],
    legal_basis: str,
    actor: User,
) -> Application:
    """`POST /applications/{id}/return` — SUBMITTED or IN_REVIEW -> RETURNED,
    so the applicant can correct and resubmit through `submit`'s own
    resubmission path (`_EDITABLE_STATUSES`, ruling 14).

    **`applications.review` (hodim) OR `applications.decide` (the head)** —
    the route's own `require_any_permission`, checked ahead of this function.
    Sending a package back for correction is not the same act as deciding it:
    the reviewer who caught an incomplete filing sends it back before the head
    ever sees it. Unlike `decision.reject`, nothing is signed here — 3.9a
    gives no ERI purpose to a reviewer, only the head holds
    `application_decision`.

    Validation runs in this order (ruling 3): the RJ code exists and TYPES a
    return, never a refusal (`_return_reason_item`); `legal_basis` is
    non-empty — `ApplicationReturnIn`'s own `min_length=1`, so a body missing
    it is 422 before this function is ever reached, the same shape
    `ApplicationRejectIn` uses; then `fields_to_fix` must be a non-empty
    object naming real columns of the application. Only once all three hold
    does the status move.

    404 `ERR-SYS-003` for an id that does not exist and for an application
    outside the caller's zone — the same answer to both, as on every staff
    route in this module (`_assert_in_actor_zone` records the RI-12 trail
    first). 409 `ERR-APP-004` in any status but SUBMITTED or IN_REVIEW.
    """
    application = await repo.get_application_for_update(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    await _assert_in_actor_zone(db, application, actor=actor, action=APPLICATION_RETURN)
    _assert_transition(application, RETURNED_STATUS)

    await _return_reason_item(db, reason_item_id)
    if not fields_to_fix:
        raise err("ERR-VAL-001", details={"reason": "fields_to_fix_required"})
    unknown = sorted(set(fields_to_fix) - _APPLICATION_FIELD_NAMES)
    if unknown:
        raise err("ERR-VAL-001", details={"reason": "unknown_field", "fields": unknown})

    await _apply_transition(
        db,
        application,
        to_status=RETURNED_STATUS,
        action=APPLICATION_RETURN,
        actor=actor,
        reason_item_id=reason_item_id,
        legal_basis=legal_basis,
        fields_to_fix=fields_to_fix,
    )
    await notifications_service.notify(
        db,
        event_code=NOTIFY_APPLICATION_RETURNED,
        recipient_user_id=await _notification_recipient(db, application),
        params={"application_number": application.number},
        object_type="application",
        object_id=application.id,
    )
    return application


# --- Task 4 (3.9b): request for information and the SLA pause ---------------

APPLICATION_REQUEST_INFO = "application.request_info"
APPLICATION_RESPOND_INFO = "application.respond_info"
# `notification_templates.event_code` — DOTTED, seeded by migration 0025
# (task 2's own), sent by `request_info` alone: `respond_info` is the
# applicant's own act and notifies nobody, the same shape `cancel` gives its
# own withdrawal.
NOTIFY_APPLICATION_INFO_REQUESTED = "application.info_requested"
PENDING_INFO_STATUS = "PENDING_INFO"
# The ONE `doc_types` item a `respond_info` attachment is filed under —
# migration `0025` seeds it (fix round 1, after review found the first draft's
# "first ACTIVE `doc_types` item" fallback resolved to `BENEFIT_DOC_TYPE_CODE`
# in every migrated database, so an unrelated `respond-info` attachment was
# indistinguishable from a benefit certificate's scan. The scan stopped being
# a gate with ruling #189, but the label still has to be honest: a reserved,
# unambiguous code, never a fallback to "the first item of some other type".
INFO_RESPONSE_DOC_TYPE_CODE = "info_response"
INFO_RESPONSE_DOCUMENT_NOTE = "Attached in response to a request for information."


def _now() -> datetime:
    """The wall clock `request_info`/`respond_info` read the pause's two
    endpoints through — patched by `tests/modules/applications/conftest.py::
    frozen_clock`, exactly as `payments.payme_router._now` is (`payments/
    conftest.py`'s own `frozen_clock`). Ruling 8's arithmetic must be provable
    against a controlled clock: a test cannot wait three real days to prove a
    three-day pause shifts the deadline by three days."""
    return datetime.now(UTC)


async def _info_response_doc_type(db: AsyncSession) -> ClassifierItem | None:
    """The ACTIVE `doc_types` item whose code is `INFO_RESPONSE_DOC_TYPE_CODE`,
    or `None` when it is missing or archived, read through `admin.repo` rather
    than a direct `classifier_items` query (CLAUDE.md: reference data is
    read-only and reached through its owner). `respond_info` FAILS CLOSED on
    `None` — never a fallback to some other item, which is the defect fix
    round 1 found."""
    classifier = await admin_repo.get_classifier_by_code(db, DOC_TYPE_CLASSIFIER_CODE)
    if classifier is None:
        return None
    items = await admin_repo.list_classifier_items(db, classifier.id)
    return next((item for item in items if item.code == INFO_RESPONSE_DOC_TYPE_CODE), None)


async def request_info(
    db: AsyncSession, application_id: uuid.UUID, *, message: str, actor: User
) -> Application:
    """`POST /applications/{id}/request-info` — SUBMITTED or IN_REVIEW ->
    PENDING_INFO, opening the `info_requests` row that pauses the SLA clock
    (ruling 8) until `respond_info` closes it.

    **`applications.review`, zone-checked** — the route's own `require_
    permission` plus `_assert_in_actor_zone` here, exactly `start_review`'s own
    two-part rule beside it: the permission answers "may this role at all",
    the zone answers "on whose rows".

    A second `request-info` while one is already open is `ERR-APP-004`
    (`reason="info_request_already_open"`): two open pauses have no
    `responded_at` to pair unambiguously with a `requested_at`, which is
    exactly the arithmetic `sla.shift_deadline` depends on staying 1:1.
    Checked BEFORE `_assert_transition` and independently of it — being
    PENDING_INFO already implies an open row (nothing else in this module
    writes one), so a bare `_assert_transition` would answer that repeat call
    with the generic `bad_transition` and hide the actual reason; every OTHER
    illegal source status still falls through to it unchanged.

    404 `ERR-SYS-003` for an id that does not exist and for an application
    outside the caller's zone — the same answer to both, as on every staff
    route in this module. 409 `ERR-APP-004` in any status but SUBMITTED or
    IN_REVIEW.
    """
    application = await repo.get_application_for_update(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    # Before the status check, and before anything is written: this commits
    # its RI-12 trail and raises 404 on a territorial refusal.
    await _assert_in_actor_zone(db, application, actor=actor, action=APPLICATION_REQUEST_INFO)

    if await repo.get_open_info_request(db, application.id) is not None:
        raise err("ERR-APP-004", details={"reason": "info_request_already_open"})
    _assert_transition(application, PENDING_INFO_STATUS)

    await repo.add_info_request(
        db,
        InfoRequest(
            application_id=application.id,
            requested_by=actor.id,
            message=message,
            requested_at=_now(),
        ),
    )
    await _apply_transition(
        db,
        application,
        to_status=PENDING_INFO_STATUS,
        action=APPLICATION_REQUEST_INFO,
        actor=actor,
        reason=message,
    )
    await notifications_service.notify(
        db,
        event_code=NOTIFY_APPLICATION_INFO_REQUESTED,
        recipient_user_id=await _notification_recipient(db, application),
        params={"application_number": application.number},
        object_type="application",
        object_id=application.id,
    )
    return application


async def respond_info(
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    text: str,
    file_ids: list[uuid.UUID],
    actor: User,
) -> Application:
    """`POST /applications/{id}/respond-info` — the OWNER's own reply:
    PENDING_INFO -> IN_REVIEW, closing the newest open `info_requests` row,
    attaching `file_ids` as `application_documents`, and RESUMING the SLA
    clock by shifting `sla_deadline_at` forward by exactly the length of the
    pause (ruling 8, `sla.shift_deadline`) — never re-derived from a fresh
    count, and never left untouched, which would let the days the office spent
    waiting on the applicant count against it.

    `_own_application_for_update` is the ownership half (404 for a stranger);
    `_assert_transition` is the status half — PENDING_INFO is the only source
    this route may leave from, so an application no longer paused is
    `ERR-APP-004` (`reason="bad_transition"`).

    Every `file_ids` entry is checked exactly as `add_document` checks its own
    `file_id` (`_own_document_file`): it must be the caller's OWN ACTIVE
    upload, never merely an id that exists. An empty list attaches nothing —
    a text-only reply is legal.
    """
    application = await _own_application_for_update(db, application_id, actor=actor)
    _assert_transition(application, IN_REVIEW_STATUS)

    info_request = await repo.get_open_info_request(db, application.id)
    # An application only reaches PENDING_INFO through `request_info`, which
    # never returns without opening exactly one, unclosed, request — the
    # invariant is this module's own two writers, not user input.
    assert info_request is not None, (
        "PENDING_INFO with no open info_requests row — request_info's own invariant broke"
    )
    now = _now()
    info_request.responded_at = now
    info_request.response_text = text

    if file_ids:
        doc_type = await _info_response_doc_type(db)
        if doc_type is None:
            raise err("ERR-APP-003", details={"reason": "doc_type_not_configured"})
        for file_id in file_ids:
            file = await _own_document_file(db, file_id, actor=actor)
            document = ApplicationDocument(
                application_id=application.id,
                doc_type_item_id=doc_type.id,
                file_id=file.id,
                uploaded_by=actor.id,
                note=INFO_RESPONSE_DOCUMENT_NOTE,
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

    if application.sla_deadline_at is not None:
        application.sla_deadline_at = sla.shift_deadline(
            application.sla_deadline_at, paused_for=now - info_request.requested_at
        )
    await _apply_transition(
        db,
        application,
        to_status=IN_REVIEW_STATUS,
        action=APPLICATION_RESPOND_INFO,
        actor=actor,
        reason=text,
    )
    return application


# --- Task 5 (3.9b): conclusions and recalculation ---------------------------

APPLICATION_CONCLUSION_ADD = "application_conclusion.add"


async def _holds(db: AsyncSession, actor: User, code: str) -> bool:
    """One permission code, `sys_admin` bypass included — `_holds_staff_read`'s
    own idiom (line ~459), narrowed to a single code: `add_conclusion` gates a
    WRITE on exactly one code per `kind`, never "any of several"."""
    if await auth_service.role_code(db, actor) == SUPERUSER_ROLE:
        return True
    return code in await auth_repo.permission_codes(db, actor)


async def add_conclusion(
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    kind: str,
    text: str,
    recommendation: str | None,
    actor: User,
) -> ApplicationConclusion:
    """`POST /applications/{id}/conclusion` — tz/04 С8: a specialist's written
    finding, on the record for the head to read before deciding. Immutable
    (ruling 10): no PATCH, no DELETE anywhere in this module — a correction is
    a NEW row (`test_a_corrected_conclusion_is_a_second_row_and_both_are_
    visible`), and `application_conclusions` carries no append-only DB trigger
    only because nothing here ever attempts an UPDATE in the first place.

    WHO may write which `kind` is not one flat rule, and the permission check
    runs BEFORE the application is even fetched — exactly as a route-level
    `require_permission` would answer before the handler ever sees the id —
    since `kind` alone already decides it and leaks nothing about which
    application is being asked about:

      * `kind="executor"` — the hodim, gated on `applications.review` and then
        zone-checked (`_assert_in_actor_zone`, the same two-part rule
        `request_info` applies beside it: the permission answers "may this
        role at all", the zone answers "on whose rows").
      * `kind="gis"` — gated on `applications.conclude_gis`, then
        zone-checked exactly like the `executor` branch above (fix round 1,
        task 5: the controller ruling that closed the gap the first draft of
        this function flagged). `app/modules/gis/permissions.py` registers no
        code that fits — only `gis.contours.manage`, `.approve` and
        `gis.layers.manage`, and `gis.contours.approve` was rejected
        explicitly (it would let a pure contour editor write conclusions on
        applications, a different authority) — so the code is owned HERE, by
        `applications`, the same reason `review`/`decide`/`assign` are too:
        the thing it authorises is a write on an APPLICATION, not on a
        contour. tz/03's matrix gives the GIS specialist unzoned "K" (read)
        on every application, but that answers WHO may look, not WHO may
        write a finding into its record — every other staff write in this
        module pairs its permission with the actor's own zone (lesson: zone
        scoping is not a permission check), and a written conclusion is a
        write, so this one is zoned the same way.
    """
    if kind == "executor":
        if not await _holds(db, actor, APPLICATIONS_REVIEW):
            raise err("ERR-ACL-001", details={"permission": APPLICATIONS_REVIEW})
    elif kind == "gis":
        if not await _holds(db, actor, APPLICATIONS_CONCLUDE_GIS):
            raise err("ERR-ACL-001", details={"permission": APPLICATIONS_CONCLUDE_GIS})
    else:
        # The schema's `Literal["executor", "gis"]` admits nothing else; kept
        # as a fail-closed default rather than an `assert`, which pyright
        # would accept but a bypassed/loosened schema would then reach as a
        # 500 instead of a 403.
        raise err("ERR-ACL-001", details={"reason": "unknown_conclusion_kind"})

    application = await repo.get_application(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    await _assert_in_actor_zone(db, application, actor=actor, action=APPLICATION_CONCLUSION_ADD)

    row = ApplicationConclusion(
        application_id=application.id,
        author_id=actor.id,
        kind=kind,
        text=text,
        recommendation=recommendation,
    )
    await repo.add_conclusion(db, row)
    await audit.log(
        db,
        action=APPLICATION_CONCLUSION_ADD,
        user_id=actor.id,
        object_type="application_conclusion",
        object_id=row.id,
        new_value={
            "application_id": str(application.id),
            "kind": kind,
            "recommendation": recommendation,
        },
    )
    return row


NOTIFY_APPLICATION_RECALCULATED = "application.recalculated"


async def recalculate(db: AsyncSession, application_id: uuid.UUID, *, actor: User) -> Calculation:
    """`POST /applications/{id}/recalculate` — ruling 17: a NEW `calculations`
    row for an application still open to review (SUBMITTED, IN_REVIEW,
    PENDING_INFO or RETURNED); from APPROVED onward it is refused, because by
    then the figure has been billed (3.10a) and, once a permit exists, printed
    on a signed document (3.11a). tz/04 С5: after the vet/cadastre checks, the
    hodim confirms the price or sends it for recalculation — a corrected
    tariff, a newly published `coef_sb` row, or a discrepancy one of those
    external checks surfaced. The GIS specialist is NOT among the actors this
    route admits (owner decision, 2026-09-05); their own `kind=gis` conclusion
    beside this route is what `design/03` grants them instead of a re-price.

    Routed entirely through `norms.service.save_calculation`, which already
    carries `_assert_application_open_for_calculation` — the actor-dependent
    WHO/WHEN guard (`applications.review`/`.decide`, plus the four statuses
    above; APPROVED-and-beyond closed to everyone) and the SUBJECT check (the
    calculation must price the application's own contour). Nothing here
    re-implements any of that (CLAUDE.md: the guard lives in `norms` and
    BOTH write paths go through it), and the refusal is `norms`' own
    state-conflict code, `ERR-NORM-005` (409) — never `ERR-APP-004`.

    `checks.calculation_payload` builds the request from the application's
    CURRENT stored fields — the same call `submit`'s own `_price` makes — and
    prices them against whatever `norms` reads as effective right now. This
    stage adds no route letting a reviewer edit those fields directly, so what
    a recalculation can change is the RATES the engine reads, not the
    application's own columns; a future stage that lets a reviewer correct the
    herd or the area mid-review reprices through this same function.
    """
    application = await repo.get_application(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    payload = await checks.calculation_payload(db, application)
    calculation = await norms_service.save_calculation(
        db,
        payload=payload.model_copy(update={"application_id": application.id}),
        actor=actor,
    )
    await notifications_service.notify(
        db,
        event_code=NOTIFY_APPLICATION_RECALCULATED,
        recipient_user_id=await _notification_recipient(db, application),
        params={"application_number": application.number},
        object_type="application",
        object_id=application.id,
    )
    return calculation


async def cancel(
    db: AsyncSession, application_id: uuid.UUID, *, reason: str | None = None, actor: User
) -> Application:
    """`POST /applications/{id}/cancel` — the applicant withdraws.

    **Legal from DRAFT, SUBMITTED and IN_REVIEW, and this route enforces that
    set ITSELF** (controller ruling R26, final review Important 3): `tz/05`
    lets an applicant withdraw at any point before a decision, and one who no
    longer wants the permit should not have to wait for one. Anything later is
    somebody else's money or somebody else's document — `ERR-APP-004`, with
    `reason="cancel_after_decision"`.

    **The transition table is deliberately WIDER than this set**, and the gap
    is not an oversight to be closed by deleting the edge. `INVOICED ->
    CANCELLED` is in `tz/05` and stage 3.10b will drive it — through
    `set_status`, from `payments`, which owns the invoice — so the edge stays.
    What may not happen is THIS route reaching it: `payments` documents its own
    lock order as invoice-then-application (`payments/service.py`,
    `payme._perform_transaction` -> `confirm_payment` -> `set_status`), while a
    cancel from INVOICED would take application-then-invoice, through
    `on_application_cancelled` running inside this transaction. That inversion
    became reachable for the first time in 3.9a — this is the first code that
    ever published `APPLICATION_CANCELLED` — and it cannot be fixed from here:
    taking the invoice lock in `applications` would be a level-3 -> level-4
    call the module boundaries forbid. Restricting the SOURCE set puts it back
    out of reach, and 3.10b lands the withdrawal-after-invoice path in the
    module that can take the two locks in the documented order.

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
    if application.status not in CANCELLABLE_BY_APPLICANT_STATUSES:
        # Ahead of `_apply_transition` and NOT delegated to it: the table would
        # let INVOICED through, and the reason has to say that the refusal is
        # about who may withdraw and when, not about a malformed edge.
        raise err(
            "ERR-APP-004",
            details={"reason": "cancel_after_decision", "status": application.status},
        )
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


# Task 6, 3.9b. A flow verb like `APPLICATION_CANCEL` above, audited under its
# own name for the same reason: it does more than move a status, it creates a
# whole new row.
APPLICATION_CLONE = "application.clone"


async def clone(db: AsyncSession, application_id: uuid.UUID, *, actor: User) -> Application:
    """`POST /applications/{id}/clone` — a fresh DRAFT pre-filled from an
    application the caller owns, in WHATEVER status it holds: a herder
    renewing next season's grazing should not have to retype the plot, the
    activity or the herd every filing.

    **The point of a clone is what it does NOT copy.** Everything the source
    EARNED by being reviewed, priced, signed or decided stays behind — the
    public `number`, `status` (always a fresh `DRAFT`), the frozen
    `contour_version_id`, every timestamp, the SLA deadline, the assignment,
    the documents, the checks, the calculation and the whole status history:
    the clone's own timeline holds exactly the one `DRAFT` row this function
    writes. Documents are excluded ON PURPOSE, not merely deferred: a
    veterinary certificate has a validity period, and silently carrying last
    year's into a new filing is exactly the kind of quiet error this system
    exists to prevent — the applicant attaches a fresh one.

    What copies is the request itself: who is filing and on whose authority
    (`applicant_id`, `on_behalf`, `representation_id`), the plot and activity
    (`contour_id`, `activity_type_id`), the declared period, area, quantity and
    herd (`period_from`, `period_to`, `requested_area_ha`, `quantity`,
    `items`) and the claimed `benefit_category_item_id`. The period comes
    along with the rest of the request rather than being left for a mandatory
    `PATCH`: `checks.REQUIRED_FOR_PRICING` refuses a submission missing it, and
    a clone an applicant cannot submit without editing fields that did not
    change (the plot, the herd) would save them nothing. `contour_version_id`
    is deliberately NOT among them: the clone reprices against whichever
    version is published at ITS OWN submission (ruling 22), never the one the
    source was decided against.

    `parent_application_id` is set to the SOURCE while `kind` stays `"new"`
    (`KIND_NEW`): a clone is a brand-new filing that happens to remember where
    it came from, not `extend` (`tz/12` #6) — that verb belongs to 3.11's
    `POST /permits/{id}/extend`, on an already-ISSUED permit, and is out of
    scope here.

    The OWNER only, in ANY status — a stranger is told 404, the same answer
    every other refusal in this module gives, never 403 (`_readable_
    application`'s own reasoning: an application carries a citizen's name,
    plot and herd from the moment it exists). Unlocked, deliberately unlike
    `_own_application_for_update`: the source is only ever READ here, never
    written, so there is nothing to serialise against a concurrent writer.

    No event is published and no notification sent: nothing subscribes to a
    clone and no template is seeded for one — inventing either here would be
    exactly the mistake `events.NOTIFIED_EVENT_CODES`'s own note on
    `application.cancelled` warns against.
    """
    source = await repo.get_application(db, application_id)
    if source is None or source.applicant_id not in await _own_applicant_ids(db, actor):
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    application = Application(
        applicant_id=source.applicant_id,
        submitted_by_user_id=actor.id,
        on_behalf=source.on_behalf,
        representation_id=source.representation_id,
        activity_type_id=source.activity_type_id,
        contour_id=source.contour_id,
        requested_area_ha=source.requested_area_ha,
        period_from=source.period_from,
        period_to=source.period_to,
        quantity=source.quantity,
        benefit_category_item_id=source.benefit_category_item_id,
        status=INITIAL_STATUS,
        channel=CHANNEL_PORTAL,
        kind=KIND_NEW,
        parent_application_id=source.id,
    )
    db.add(application)
    await db.flush()
    for item in await repo.list_items(db, source.id):
        db.add(
            ApplicationItem(
                application_id=application.id,
                livestock_type_id=item.livestock_type_id,
                head_count=item.head_count,
            )
        )
    await repo.add_status_history(
        db,
        ApplicationStatusHistory(
            application_id=application.id,
            from_status=None,
            to_status=INITIAL_STATUS,
            changed_by=actor.id,
        ),
    )
    # `created_at`/`updated_at`/`requested_area_ha`/`quantity` all round-trip
    # through Postgres defaults or `NUMERIC`'s own scale (the same lesson
    # `create_draft` and `patch_draft` both carry) — refreshed before this row
    # is serialized into the 201 response.
    await db.refresh(application)
    await audit.log(
        db,
        action=APPLICATION_CLONE,
        user_id=actor.id,
        object_type="application",
        object_id=application.id,
        old_value={"parent_application_id": str(source.id)},
        new_value=_snapshot(application, await repo.list_items(db, application.id)),
    )
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

    **`info_requests` is populated (final whole-branch review, IMPORTANT)**:
    open or closed, oldest first (`repo.list_info_requests`). The pause it
    records is the one event on this branch that silently moves a
    legally-consequential deadline (`sla_deadline_at`, `sla.shift_deadline`),
    and this is the only audit view an inspector or the applicant reads — a
    deadline that jumped with no explanation anywhere in the timeline was the
    actual defect the empty list used to hide, not merely an unfinished
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
        "info_requests": await repo.list_info_requests(db, application.id),
    }


# --- Task 7 (3.9b): external checks — veterinary and cadastre ---------------
#
# tz/04 С5: after start-review, the office checks the application against the
# veterinary registry and the cadastre. Both routes below carry
# `applications.review` alone (router.py) — maker and confirmer are the SAME
# role here, unlike `norms.service.publish_versioned`'s maker/checker split
# across two different codes, so there is only one to gate on; the identity
# comparison in `confirm_check` is what tells the two calls apart (lesson: "A
# maker-checker route needs BOTH roles' permission").
#
# `check_type in ("vet", "cadastre")` only — the auto GIS/norm checks
# (`gis_validity`, `norm_season`, ...) are `checks.run_all`'s alone, written
# with `source="auto"`, and this route never touches them.

APPLICATION_CHECK_ADD = "application_check.add"
APPLICATION_CHECK_CONFIRM = "application_check.confirm"


async def _external_check(
    check_type: str, *, application_id: uuid.UUID
) -> VetCheckResult | CadastreCheckResult:
    """Dispatches to whichever adapter `ApplicationCheckIn.check_type` named —
    `vet_adapter`/`cadastre_adapter`'s own `get_adapter()` factory, exactly
    the shape `oneid.get_oneid_adapter`/`otp_sender.get_otp_sender` already
    use. A plain `if`/`else` rather than a dict of callables: the schema's
    `Literal["vet", "cadastre"]` already admits nothing else, and this way
    every adapter's own result type stays visible to pyright.

    **Final whole-branch review**: `get_adapter()` raises a bare
    `NotImplementedError` on two paths — `app_env=prod` refusing to answer
    from a mock, or `*_MODE=real` naming a contract that has not been
    written — and left uncaught here that reached `app.main`'s generic
    handler as an unhandled `ERR-SYS-001` 500 with a traceback: a crash where
    the ruling meant an honest refusal. Both paths leave the office with the
    identical remedy, `add_check`'s `source="manual_fallback"` branch, so
    both are translated the same way, into `ERR-APP-004` with a `reason` —
    the same shape this module's other typed refusals on this exact route
    already carry (`not_manual_fallback`, `already_confirmed`, ...)."""
    try:
        if check_type == "vet":
            return await vet_adapter.get_adapter().check(application_id=application_id)
        return await cadastre_adapter.get_adapter().check(application_id=application_id)
    except NotImplementedError as exc:
        raise err(
            "ERR-APP-004",
            details={"reason": "live_check_unavailable", "check_type": check_type},
        ) from exc


async def _assert_check_doc_active(db: AsyncSession, file_id: uuid.UUID) -> None:
    """Existence + active only — `gis.service._assert_approval_doc_active`'s
    own shape (lesson: "An existence check is not a validity check"). The
    scanned paper result may legitimately be uploaded by office staff other
    than the one filing this check, so `_own_document_file`'s ownership
    requirement does not apply here."""
    file = await db.get(MediaFile, file_id)
    if file is None or file.status != "active":
        raise err("ERR-VAL-001", details={"reason": "check_doc_not_found"})


async def add_check(
    db: AsyncSession,
    application_id: uuid.UUID,
    payload: ApplicationCheckIn,
    *,
    actor: User,
) -> ApplicationCheck:
    """`POST /applications/{id}/checks` — either calls the live vet/cadastre
    adapter (`source="external_api"`), or records a paper result under
    maker-checker (`source="manual_fallback"`, Oybek's ruling, 2026-09-05: the
    paper fallback is exactly the case a second pair of eyes exists for). A
    manual row is always written `confirmed_by=None`; `confirm_check` below is
    the only path that ever sets it.

    404 `ERR-SYS-003` for an id that does not exist or an application outside
    the caller's zone — `_assert_in_actor_zone`, before anything is written,
    the same two-part rule every staff write in this module applies (permission
    is the route's, zone is here). 422 `ERR-VAL-001` for the manual shape
    missing `result` or `doc_file_id`, or naming a `doc_file_id` that is
    missing or archived.
    """
    application = await repo.get_application(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    await _assert_in_actor_zone(db, application, actor=actor, action=APPLICATION_CHECK_ADD)

    if payload.source == "manual_fallback":
        if payload.result is None or payload.doc_file_id is None:
            raise err(
                "ERR-VAL-001",
                details={"reason": "manual_fallback_requires_result_and_doc_file_id"},
            )
        await _assert_check_doc_active(db, payload.doc_file_id)
        row = ApplicationCheck(
            application_id=application.id,
            check_type=payload.check_type,
            result=payload.result,
            details={"source": "manual_fallback"},
            source="manual_fallback",
            doc_file_id=payload.doc_file_id,
            created_by=actor.id,
        )
    else:
        verdict = await _external_check(payload.check_type, application_id=application.id)
        # Decision #46 ruling 9: a synchronous adapter call logs in the
        # CALLER's transaction (mock-only today), never a separate session.
        await integrations_service.log_integration(
            db,
            direction="out",
            system=payload.check_type,
            endpoint=str(application.id),
            meta={"result": verdict.result},
        )
        row = ApplicationCheck(
            application_id=application.id,
            check_type=payload.check_type,
            result=verdict.result,
            details=verdict.details,
            source="external_api",
            created_by=actor.id,
        )
    await repo.add_checks(db, [row])
    await audit.log(
        db,
        action=APPLICATION_CHECK_ADD,
        user_id=actor.id,
        object_type="application_check",
        object_id=row.id,
        new_value={
            "application_id": str(application.id),
            "check_type": row.check_type,
            "source": row.source,
            "result": row.result,
        },
    )
    return row


async def confirm_check(
    db: AsyncSession, application_id: uuid.UUID, check_id: uuid.UUID, *, actor: User
) -> ApplicationCheck:
    """`POST /applications/{id}/checks/{check_id}/confirm` — the SECOND
    person a paper result needs before it is usable. Stage 3.7's tariff
    maker-checker is the identical rule (`norms.service.publish_versioned`):
    the maker of THIS row may not also be its confirmer.

    404 `ERR-SYS-003` for an id that does not exist, an application outside
    the caller's zone, or a `check_id` that does not belong to this
    application. 409 `ERR-APP-004` for a row that is not
    `source="manual_fallback"` (`reason="not_manual_fallback"` — there is
    nothing to confirm on an automatic or external-api check), one already
    confirmed (`reason="already_confirmed"`), or a confirmer who is also its
    own maker (`reason="maker_cannot_confirm_own_record"`).
    """
    application = await repo.get_application(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    await _assert_in_actor_zone(db, application, actor=actor, action=APPLICATION_CHECK_CONFIRM)

    check = await repo.get_check(db, check_id)
    if check is None or check.application_id != application.id:
        raise err("ERR-SYS-003", details={"check": str(check_id)})
    if check.source != "manual_fallback":
        raise err("ERR-APP-004", details={"reason": "not_manual_fallback"})
    if check.confirmed_by is not None:
        raise err("ERR-APP-004", details={"reason": "already_confirmed"})
    if check.created_by == actor.id:
        raise err("ERR-APP-004", details={"reason": "maker_cannot_confirm_own_record"})

    check.confirmed_by = actor.id
    check.confirmed_at = datetime.now(UTC)
    await audit.log(
        db,
        action=APPLICATION_CHECK_CONFIRM,
        user_id=actor.id,
        object_type="application_check",
        object_id=check.id,
        new_value={"confirmed_by": str(actor.id)},
    )
    return check


# --- Stage 7.6: the open-work seam (finding F4/F5, plan 07.6 tasks 2 and 3) --
#
# `admin` is level 1 and may not import this module (level 3), so the two
# questions it needs answered — "what does this USER still hold" (F4,
# `delete_user`) and "what does this ORGANIZATION still have hanging off it"
# (F5, `archive_organization`) — travel the other way, as a registration into
# `admin.open_work`'s two registries. `app/event_subscriptions.py` is the ONE
# place that performs the registration (never here, and never `main.py` —
# the standalone worker imports this module too and must see the same
# answer). Both providers share this cap so a refusal's `details.open_work`
# never lists more ids than an admin can act on in one sitting.
OPEN_WORK_ID_CAP = 20


async def open_work_provider(
    db: AsyncSession, user_id: uuid.UUID
) -> admin_open_work.OpenWork | None:
    """`admin.open_work.OPEN_WORK_PROVIDERS`'s entry for this module.

    Non-terminal only (`TERMINAL_APPLICATION_STATUSES`, ruling R5): an
    application in a terminal status needs nobody to act, and a guard that
    counted those would make a long-serving reviewer undeletable forever —
    exactly the failure mode `test_a_terminal_application_does_not_block`
    pins.
    """
    ids = await repo.assigned_open_application_ids(
        db,
        user_id,
        exclude_statuses=TERMINAL_APPLICATION_STATUSES,
        limit=OPEN_WORK_ID_CAP,
    )
    if not ids:
        return None
    return admin_open_work.OpenWork(kind="applications", count=len(ids), ids=list(ids))


async def open_work_provider_for_org(
    db: AsyncSession, organization_id: uuid.UUID
) -> admin_open_work.OpenWork | None:
    """`admin.open_work.ORG_WORK_PROVIDERS`'s entry for this module (F5): every
    non-terminal application currently routed to `organization_id` through its
    OWN `assigned_org_id` — deliberately not the contour's owner
    (`_effective_organization`'s other half), which would make archiving a
    leshoz depend on geometry this check has no business reading.
    """
    ids = await repo.assigned_open_application_ids_for_org(
        db,
        organization_id,
        exclude_statuses=TERMINAL_APPLICATION_STATUSES,
        limit=OPEN_WORK_ID_CAP,
    )
    if not ids:
        return None
    return admin_open_work.OpenWork(kind="applications", count=len(ids), ids=list(ids))
