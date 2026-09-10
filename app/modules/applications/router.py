"""Application routes (design/03 § Заявки; plan 03.9a task 3).

Importing `permissions` below is what registers this module's five codes — the
same idiom every other router uses, and the reason `app/main.py` carried a
stand-in import of that module while branch 1 had no router at all
(`tests/test_permissions_registry.py` fails on a granted code the registry never
learned).

**Why only the write routes carry `require_permission`.** `POST` and `PATCH` are
the applicant's own actions and `applications.create` is a role grant on
`applicant` (migration 0015), so the dependency is the right gate there. The two
READ routes take `Depends(get_current_user)` instead: each also admits the
application's own applicant, who holds none of the staff codes, so a route-level
`require_permission` would reject a citizen reading their own draft before the
ownership check ever ran. Both halves of the read rule — the permission and the
zone — are applied inside the service (`_readable_application`,
`list_applications`), which is exactly how `GET /permits` and `GET /signatures`
are gated, and for exactly this reason.

No `Idempotency-Key` on `POST /applications`, deliberately. The mechanism
(`auth.deps.idempotency_context`) belongs on `POST /applications/{id}/submit`
(task 5), where a replay would mint a second public number for one filing. A
replayed create produces a second EMPTY draft, which costs a row and confuses
nobody: the applicant sees two drafts and abandons one.
"""

import base64
import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.idempotency import IdempotencyContext
from app.core.schemas import Page, PageParams
from app.modules.applications import decision as service_decision
from app.modules.applications import service
from app.modules.applications.permissions import (
    APPLICATIONS_ASSIGN,
    APPLICATIONS_CREATE,
    APPLICATIONS_DECIDE,
    APPLICATIONS_REVIEW,
)
from app.modules.applications.schemas import (
    ApplicationApproveIn,
    ApplicationAssignIn,
    ApplicationCalculationOut,
    ApplicationCancelIn,
    ApplicationCardOut,
    ApplicationCheckIn,
    ApplicationCheckOut,
    ApplicationCloneOut,
    ApplicationConclusionIn,
    ApplicationConclusionOut,
    ApplicationDecisionOut,
    ApplicationDocumentIn,
    ApplicationDocumentOut,
    ApplicationFileIn,
    ApplicationFilingIn,
    ApplicationOut,
    ApplicationPatch,
    ApplicationRejectIn,
    ApplicationRequestInfoIn,
    ApplicationRespondInfoIn,
    ApplicationReturnIn,
    ApplicationStatus,
    ApplicationSubmitIn,
    ApplicationTimelineOut,
    FilingPackageOut,
    PrecheckCalculationOut,
    PrecheckCheckOut,
    PrecheckOut,
)
from app.modules.auth.deps import (
    get_current_user,
    idempotency_context,
    require_any_permission,
    require_permission,
)
from app.modules.auth.models import User

# `applications.number` is `RX-<yyyy>-<seq>` (plan ruling 5а). Bounded because
# it is bound into SQL as text from a query string anybody can type; the value
# itself is matched exactly, never as a pattern.
NUMBER_MAX_LENGTH = 64

router = APIRouter(tags=["applications"])


# --- Stage 12: the stateless pre-check and package ---------------------------
#
# Registered BEFORE every `/applications/{application_id}…` route: FastAPI
# matches in declaration order, and `application_id` is a `uuid.UUID`, so a
# `POST /applications/precheck` reaching a parametrised route first would be
# answered 422 for a path segment that is not a uuid.


@router.post("/applications/precheck")
async def precheck_filing(
    payload: ApplicationFilingIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_CREATE))],
) -> PrecheckOut:
    """The dry run over a filing that exists only in this body (plan 12, R3):
    the checks as data and the price, nothing stored. 200 even when a check
    blocks; an incomplete filing answers `skipped` rows naming the fields;
    422 `ERR-VAL-001` for an unknown reference, a filing naming somebody
    else's applicant, or a document that is not the caller's own upload; 422
    `ERR-NORM-004` for an unpublished rule parameter."""
    card = await service.precheck_filing(db, payload, actor=actor)
    priced = card["calculation"]
    return PrecheckOut(
        checks=[PrecheckCheckOut(check_type=c, result=r, details=d) for c, r, d in card["checks"]],
        calculation=None if priced is None else PrecheckCalculationOut.build(priced),
    )


@router.post("/applications/package")
async def package_filing(
    payload: ApplicationFilingIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_CREATE))],
) -> FilingPackageOut:
    """The canonical bytes to sign over a filing that has no row yet, and the
    `application_id` those bytes name (plan 12, R2) — the client signs the
    bytes and posts both to `POST /applications`. Only a legal entity needs
    this: a citizen's simple signature (#183) is taken by the server.

    400 `ERR-APP-001` naming the fields still to fill; 409 `ERR-GIS-005` for a
    contour with no published version; 422 `ERR-NORM-004`."""
    application_id, package = await service.package_filing(db, payload, actor=actor)
    return FilingPackageOut(
        application_id=application_id, package=base64.b64encode(package).decode("ascii")
    )


@router.post("/applications", status_code=201)
async def file_application(
    payload: ApplicationFileIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_CREATE))],
    ctx: Annotated[IdempotencyContext, Depends(idempotency_context)],
) -> ApplicationOut:
    """The whole filing in one request → 201, the application SUBMITTED with
    its public number (plan 12, R1). `Idempotency-Key` is MANDATORY (422
    `ERR-VAL-001` `idempotency_key_required`, 409 `ERR-SYS-005` on a
    conflicting replay) — a replayed filing would mint a second number; `ctx`
    is declared AFTER `actor` so the one `get_current_user` resolves once.

    400 `ERR-APP-001` naming the fields still to fill (`rules_accepted` among
    them); 422 `ERR-VAL-001` (`package_id_required` / `package_id_unexpected`,
    an unknown reference, somebody else's applicant, a document that is not
    the caller's upload); 422 `ERR-APP-003` (benefit claim); 409 `ERR-GIS-005`
    (no published version); `ERR-GIS-*`/`ERR-NORM-*` for a blocking check; 422
    `ERR-SIGN-001` (bad envelope, `package_changed`,
    `simple_signature_not_allowed`); 409 `ERR-APP-002` with the existing
    number for an overlapping active filing; 409 `ERR-APP-004` `already_filed`
    for an id that already has a row.
    """
    application = await service.file(
        db, payload, actor=actor, ip=request.client.host if request.client else None
    )
    out = ApplicationOut.model_validate(application)
    await ctx.save(db, status_code=201, body=out.model_dump(mode="json"))
    return out


@router.patch("/applications/{application_id}")
async def patch_application(
    application_id: uuid.UUID,
    payload: ApplicationPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_CREATE))],
) -> ApplicationOut:
    """Any subset of the draft's own fields; `items` is replaced wholesale.

    `applications.create` is the gate, not a separate "edit" code: filing and
    editing one's own application are one right (its own registry description
    says «File and edit one's own application»). Ownership is the service's
    check, so a holder of the code who is not the owner gets 404 — never a 403,
    which would confirm the application exists.

    409 `ERR-APP-004` in any status but DRAFT; 422 `ERR-VAL-001` for an unknown
    reference id, and for `requested_area_ha`, which is frozen at submission
    (ruling 22) and refused here as an unknown field.
    """
    return ApplicationOut.model_validate(
        await service.patch_draft(db, application_id, payload, actor=actor)
    )


@router.get("/applications")
async def list_applications(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    params: Annotated[PageParams, Depends()],
    status: ApplicationStatus | None = None,
    activity_type_id: uuid.UUID | None = None,
    contour_id: uuid.UUID | None = None,
    applicant_id: uuid.UUID | None = None,
    number: Annotated[str | None, Query(max_length=NUMBER_MAX_LENGTH)] = None,
    period_from: date | None = None,
    period_to: date | None = None,
) -> Page[ApplicationOut]:
    """The applications this caller may see: their own, or — holding one of the
    three staff read codes — their zone's.

    A caller entitled to nothing gets an empty page, never a 403: a list has no
    row to refuse, and nobody named a target.

    `status` is the `ApplicationStatus` literal, so a typo is a 422 rather than
    an empty page that reads as "no applications in that state".
    `period_from`/`period_to` select applications whose own period OVERLAPS the
    window — the question a reviewer's queue asks.
    """
    items, total = await service.list_applications(
        db,
        actor=user,
        params=params,
        status=status,
        activity_type_id=activity_type_id,
        contour_id=contour_id,
        applicant_id=applicant_id,
        number=number,
        period_from=period_from,
        period_to=period_to,
    )
    return Page[ApplicationOut](
        items=[ApplicationOut.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/applications/{application_id}")
async def get_application_card(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> ApplicationCardOut:
    """The application, its items, its documents, its checks and its current
    price.

    404 `ERR-SYS-003` for an id that does not exist, for an application this
    caller has no claim on, and for one outside a staff caller's zone — the same
    answer to all three on purpose, since anything else makes this route an
    application-existence oracle for a document full of personal data. The
    territorial refusal is recorded as RI-12 before it answers.
    """
    return ApplicationCardOut.build(await service.get_card(db, application_id, actor=user))


# --- Task 4: documents and the pre-check --------------------------------------
#
# All three carry `require_permission(APPLICATIONS_CREATE)`, the same gate as
# `POST`/`PATCH` above and for the same reason: attaching a document and asking
# what a draft would cost are both part of «file and edit one's own
# application», the registry's own description of that code. Ownership is the
# service's check, so a holder who is not the owner gets 404 — never a 403,
# which would confirm the application exists.
#
# No `Idempotency-Key` on the pre-check either, and deliberately: a repeat check
# is a NEW row by ruling 12 — the reviewer has to see that a contour passed at
# submission even if it would fail today — so a replayed pre-check producing a
# second set of rows is the SPECIFIED behaviour, not the duplicate the mechanism
# exists to suppress. It belongs on `POST /applications/{id}/submit` (task 5),
# where a replay would mint a second public number for one filing.


@router.post("/applications/{application_id}/documents", status_code=201)
async def attach_document(
    application_id: uuid.UUID,
    payload: ApplicationDocumentIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_CREATE))],
) -> ApplicationDocumentOut:
    """201 with the attachment. The bytes are uploaded through `POST /files`
    first and this route stores only the reference — checked to exist, to be
    active, and to be the caller's OWN upload, because a file id an applicant
    supplies is untrusted input.

    409 `ERR-APP-004` in any status but DRAFT; 422 `ERR-VAL-001` for a
    `doc_type_item_id` outside the `doc_types` classifier
    (`unknown_doc_type`) and for a file that is missing, archived or somebody
    else's (`document_file_not_found` / `document_file_not_owned`).
    """
    return ApplicationDocumentOut.model_validate(
        await service.add_document(db, application_id, payload, actor=actor)
    )


@router.delete("/applications/{application_id}/documents/{document_id}", status_code=204)
async def detach_document(
    application_id: uuid.UUID,
    document_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_CREATE))],
) -> None:
    """204. DRAFT only, and both ids are checked — a document belonging to a
    different application is 404, not a cross-application delete. The
    `media_files` row survives: files are never deleted in this system."""
    await service.remove_document(db, application_id, document_id, actor=actor)


@router.post("/applications/{application_id}/precheck")
async def precheck_application(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_CREATE))],
) -> PrecheckOut:
    """A dry run: writes the `application_checks` rows and answers with them
    plus the price. **The status never moves and no calculation is stored**
    (ruling 8 — the one that is stored is written at submission and is what
    3.10 invoices from).

    **200 even when a check BLOCKS.** A failing GIS or norm result is in
    `checks`, as data (design/03): an applicant must be able to see that the
    herd is over the limit, not merely be refused. Task 5's `submit` runs the
    identical `checks.run_all` and turns that same result into a 4xx — that
    difference is the whole point of having both.

    A broken INPUT is still an error here: 422 `ERR-VAL-001` for a reversed or
    over-long period, 422 `ERR-NORM-004` for a rule parameter that is not
    published (on a fresh database that is the ten `coef_sb:*` rows, which ship
    as drafts until VMQ 689 annex 5 arrives). An incomplete draft is neither —
    it answers 200 with `skipped` rows naming the fields still to fill and a
    null `calculation`.
    """
    card = await service.precheck(db, application_id, actor=actor)
    priced = card["calculation"]
    return PrecheckOut(
        checks=[
            PrecheckCheckOut(check_type=row.check_type, result=row.result, details=row.details)
            for row in card["checks"]
        ],
        calculation=None if priced is None else PrecheckCalculationOut.build(priced),
    )


# --- Task 5: the package and the submission -----------------------------------
#
# `GET /package` is NOT in design/03 — it was missed there, and every ERI flow
# needs it: a client cannot produce a detached PKCS#7 over bytes it has never
# seen. Task 9 adds it to the contracts.
#
# `POST /submit` is the ONE route in this module that carries an
# `Idempotency-Key`, and the mechanism (3.4's `auth.deps.idempotency_context`)
# lands here rather than on `POST /applications` or the pre-check for a
# concrete reason: a replayed submission would mint a SECOND public number for
# one filing, out of a counter design/03 requires to be continuous within a
# year. A replayed create makes a second empty draft, which costs a row; a
# replayed pre-check writes a second set of check rows, which ruling 12 says is
# the SPECIFIED behaviour.


@router.get("/applications/{application_id}/package")
async def get_application_package(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> Response:
    """The canonical bytes to be signed, `application/octet-stream`. The client
    signs exactly these and posts the detached PKCS#7 to `/submit`.

    `Depends(get_current_user)` and not `require_permission`, like the two
    other read routes and for the same reason: this also admits staff in zone,
    who hold no `applications.create`. Both halves of the read rule live in the
    service, and a stranger is told 404 — never 403, which would confirm the
    application exists.

    400 `ERR-APP-001` naming the fields still to fill; 409 `ERR-GIS-005` when
    the contour's geometry is still a draft; 422 `ERR-NORM-004` when a rule
    parameter is not published (on a fresh database, the ten `coef_sb:*` rows).

    **RULING 23:** these bytes are priced afresh on every call, at
    `business_today()` and against whatever tariffs and БҲМ are effective right
    then. A tariff, a `rule_parameter`, a norm or midnight in Tashkent moving
    between this call and the POST changes them, and the applicant then meets
    `ERR-SIGN-001` for something they did not do. Accepted for 3.9a (Oybek's
    choice, option в) and 3.9b's to fix — do not cache the package here.
    """
    return Response(
        content=await service.package(db, application_id, actor=user),
        media_type="application/octet-stream",
    )


@router.post("/applications/{application_id}/submit")
async def submit_application(
    application_id: uuid.UUID,
    payload: ApplicationSubmitIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_CREATE))],
    ctx: Annotated[IdempotencyContext, Depends(idempotency_context)],
) -> ApplicationOut:
    """DRAFT -> SUBMITTED, with the public number, in one transaction.

    `Idempotency-Key` is MANDATORY (422 `ERR-VAL-001`
    `idempotency_key_required` without one, 409 `ERR-SYS-005` on a conflicting
    replay). `ctx` is declared AFTER `actor` — mirroring
    `gis/imports_router.py::create_import` and `payments/router.py::
    create_pay_intent` — so the single `get_current_user` both depend on is
    resolved once; `ctx.save()` runs before the response so a replay returns
    the stored 200 rather than allocating a second number.

    400 `ERR-APP-001` (missing fields, NAMED — ruling #184's `rules_accepted`
    is one of them, `false` unless the caller explicitly sends `true`); 409
    `ERR-APP-004` in any status but DRAFT; 422 `ERR-APP-003` for a benefit
    claim with no certificate number, no supporting document, or one the
    auto-verifier seam (ruling #182) reports `unknown`/`not_yours`; 409
    `ERR-GIS-005` for a contour with no published version; `ERR-GIS-001/002/005`
    or `ERR-NORM-001/002/003/006` when a BLOCKING check fails — the difference
    from the pre-check, which reports the identical result as data; 422
    `ERR-SIGN-001` for an invalid signature, or (ruling #183) `pkcs7` absent on
    a `on_behalf="legal"` filing (`simple_signature_not_allowed`); 409
    `ERR-APP-002` with the existing number when another active application
    already covers this plot and period.
    """
    application = await service.submit(
        db,
        application_id,
        pkcs7=payload.pkcs7,
        rules_accepted=payload.rules_accepted,
        actor=actor,
        ip=request.client.host if request.client else None,
    )
    out = ApplicationOut.model_validate(application)
    await ctx.save(db, status_code=200, body=out.model_dump(mode="json"))
    return out


# --- Task 6: taking into work, cancelling, and the timeline -------------------
#
# The three gates differ, and each is the narrowest one that fits:
#
#   * `start-review` is a STAFF action — `applications.review`, the code
#     migration 0015 grants `executor_staff`. The zone is the other half of the
#     rule (ruling 14: any reviewer in the zone may take it) and lives in the
#     service, where the application's own organization can be resolved;
#   * `cancel` is the applicant's own action, so it carries `applications.
#     create` like `PATCH` and `submit` — «file and edit one's own application»
#     is one right, and ownership is the service's check;
#   * `timeline` is a READ that also admits the applicant, who holds none of the
#     staff codes, so it takes `get_current_user` and both halves of the read
#     rule live in `service._readable_application` — exactly like the card and
#     the package beside it.
#
# No `Idempotency-Key` on either POST. The mechanism belongs where a replay
# would allocate something scarce: a second public number on `submit`. A
# replayed `start-review` finds the application already IN_REVIEW and answers
# 409; a replayed `cancel` finds it already CANCELLED and answers 409, since
# `APPLICATION_TRANSITIONS` has no self-loop.


@router.post("/applications/{application_id}/start-review")
async def start_review_application(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_REVIEW))],
) -> ApplicationOut:
    """SUBMITTED -> IN_REVIEW, with the `application_assignments` row saying who
    holds it.

    404 `ERR-SYS-003` for an id that does not exist AND for an application
    outside the caller's zone — the same answer to both on purpose, since
    anything else makes this route an application-existence oracle; the
    territorial refusal is recorded as RI-12 before it answers. 409
    `ERR-APP-004` in any status but SUBMITTED.

    **design/03 also says «an incomplete package is returned immediately with
    RJ-01». That is 3.9b's**: returning needs the `RETURNED` status and the
    return route, neither of which exists in 3.9a, so an incomplete package
    reaches a human here rather than bouncing.
    """
    return ApplicationOut.model_validate(
        await service.start_review(db, application_id, actor=actor)
    )


@router.post("/applications/{application_id}/cancel")
async def cancel_application(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_CREATE))],
    payload: ApplicationCancelIn | None = None,
) -> ApplicationOut:
    """The applicant withdraws: DRAFT, SUBMITTED or IN_REVIEW -> CANCELLED, with
    an optional free-text reason.

    **The BODY is optional too, not merely its one field** — `design/03` writes
    it as `{reason?}`, and a required body made a reasonless withdrawal a 422
    for a citizen who owes nobody an explanation. `None` and `{}` mean the same
    thing here; the parameter moves after the dependencies because a defaulted
    one cannot precede them.

    From IN_REVIEW deliberately (`tz/05`): an applicant who no longer wants the
    permit should not have to wait for a decision. A cancelled application also
    stops blocking the plot — `ex_applications_no_duplicate`'s WHERE clause
    excludes CANCELLED — so the citizen can refile immediately.

    404 `ERR-SYS-003` when the caller does not own it; 409 `ERR-APP-004`
    (`reason="cancel_after_decision"`) in any other status — including
    INVOICED, which `APPLICATION_TRANSITIONS` allows and this route does not
    (controller ruling R26; `service.cancel`'s docstring says who drives that
    edge instead) — and for a repeat cancel of an already-cancelled
    application.
    """
    return ApplicationOut.model_validate(
        await service.cancel(
            db, application_id, reason=None if payload is None else payload.reason, actor=actor
        )
    )


@router.get("/applications/{application_id}/clone")
async def clone_application_template(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_CREATE))],
) -> ApplicationCloneOut:
    """200 with the FILING a caller would send to refile an application they
    own, in whatever status it holds (stage 12, plan 12 R6: a read, since
    there is no draft to create) — so a herder renewing next season's grazing
    does not retype the plot, the activity or the herd. Post it, edited or
    not, to `POST /applications`.

    `applications.create` is the gate, the same one `POST /applications`
    itself uses. Ownership is the service's own check, so a holder of the
    code who does not own the source gets 404 — never a 403, which would
    confirm the application exists (`service.clone_template`'s own docstring
    has the field-by-field account of what is carried over and what is
    deliberately left behind).
    """
    return await service.clone_template(db, application_id, actor=actor)


@router.get("/applications/{application_id}/timeline")
async def get_application_timeline(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> ApplicationTimelineOut:
    """The transitions, the assignments, the signatures and the information
    requests.

    A SUBMISSION signature sits on its own `status_history` entry (ruling 25:
    the history row's id IS the signed object's id); the top-level `signatures`
    is the DECISION line, and is empty until task 7's approve/reject signs one.
    `info_requests` lists every pause this application has had, open or closed,
    oldest first.

    404 `ERR-SYS-003` for an id that does not exist, for an application this
    caller has no claim on, and for one outside a staff caller's zone — the same
    answer to all three, as on the card.
    """
    return ApplicationTimelineOut.build(await service.timeline(db, application_id, actor=user))


# --- Task 1 (3.9b): manual reassignment ----------------------------------------
#
# `applications.assign` is granted to `sys_admin` and to NOBODY else
# (migration 0015's `ROLE_GRANTS`; Task 1 ANSWERED (б), 2026-09-05 —
# reassignment is an administrator's action, logged and rare, not the leshoz
# head's, whatever `design/03` and ruling 13's own prose still say). No zone
# check on the route or in the service: the one role that can reach it at all
# is already unrestricted nationwide (decision #41 ruling 2).


@router.post("/applications/{application_id}/assign")
async def assign_application(
    application_id: uuid.UUID,
    payload: ApplicationAssignIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_ASSIGN))],
) -> ApplicationOut:
    """Name who holds an application, superseding whatever assignment it has
    now — or claiming one auto-assignment left with no reviewer.

    403 `ERR-ACL-001` for anyone but `sys_admin`, from the dependency, before
    the service is ever reached. 404 `ERR-SYS-003` for an id that does not
    exist. 409 `ERR-APP-004` (`reason="no_organization"`) for a DRAFT with no
    contour yet — unreachable once an application is genuinely SUBMITTED.
    """
    return ApplicationOut.model_validate(
        await service.assign(
            db, application_id, user_id=payload.user_id, reason=payload.reason, actor=actor
        )
    )


# --- Task 3 (3.9b): return for correction --------------------------------------
#
# `applications.review` (hodim) OR `applications.decide` (the head) —
# `require_any_permission`, because sending a package back for correction is
# not the head's decision alone the way approve/reject are: the reviewer who
# caught an incomplete filing sends it back before the head ever sees it. The
# zone is the other half of the rule and lives in the service
# (`service._assert_in_actor_zone`), exactly like start-review beside it.
#
# No `Idempotency-Key`: a replay finds the application no longer SUBMITTED or
# IN_REVIEW (already RETURNED) and answers 409 — the same reasoning task 6's
# two POSTs give for carrying none.


@router.post("/applications/{application_id}/return")
async def return_application(
    application_id: uuid.UUID,
    payload: ApplicationReturnIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[
        User, Depends(require_any_permission(APPLICATIONS_REVIEW, APPLICATIONS_DECIDE))
    ],
) -> ApplicationOut:
    """SUBMITTED or IN_REVIEW -> RETURNED, with a typed reason, the fields to
    fix and a legal basis — so the applicant can correct and resubmit.

    422 `ERR-VAL-001`: `unknown_rejection_reason` for a `reason_item_id`
    outside the `rejection_reasons` classifier; `reason_not_returnable` for
    one that IS in it but types a refusal or a withdrawal rather than a return
    (RJ-03 is a REFUSAL — returning under it would misdescribe the decision);
    `fields_to_fix_required` for an empty object; `unknown_field` for a key
    naming no real column of the application. 404 `ERR-SYS-003` for an id that
    does not exist and for an application outside the caller's zone. 409
    `ERR-APP-004` in any status but SUBMITTED or IN_REVIEW.
    """
    return ApplicationOut.model_validate(
        await service.return_to_applicant(
            db,
            application_id,
            reason_item_id=payload.reason_item_id,
            fields_to_fix=payload.fields_to_fix,
            legal_basis=payload.legal_basis,
            actor=actor,
        )
    )


# --- Task 4 (3.9b): request for information and the SLA pause -----------------
#
# `request-info` carries `applications.review` alone — the same reviewer who
# may take an application into work may ask it a question — with the zone
# check living in the service exactly like `start-review` beside it.
# `respond-info` carries `applications.create`, the applicant's own gate
# (`patch_application`'s own reasoning above): ownership is the service's
# check, so a stranger gets 404 rather than a 403 confirming the application
# exists.
#
# No `Idempotency-Key` on either: a replayed `request-info` finds one already
# open and answers 409 (the same reasoning `return`'s own comment gives); a
# replayed `respond-info` finds the application no longer PENDING_INFO
# (already IN_REVIEW) and answers 409 too.


@router.post("/applications/{application_id}/request-info")
async def request_info_application(
    application_id: uuid.UUID,
    payload: ApplicationRequestInfoIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_REVIEW))],
) -> ApplicationOut:
    """SUBMITTED or IN_REVIEW -> PENDING_INFO, opening the `info_requests` row
    that pauses the SLA clock (ruling 8) until `respond-info` closes it.

    404 `ERR-SYS-003` for an id that does not exist and for an application
    outside the caller's zone. 409 `ERR-APP-004` in any status but SUBMITTED
    or IN_REVIEW, and (`reason="info_request_already_open"`) for a second
    request while one is already open — two open pauses would make the pause
    arithmetic ambiguous.
    """
    return ApplicationOut.model_validate(
        await service.request_info(db, application_id, message=payload.message, actor=actor)
    )


@router.post("/applications/{application_id}/respond-info")
async def respond_info_application(
    application_id: uuid.UUID,
    payload: ApplicationRespondInfoIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_CREATE))],
) -> ApplicationOut:
    """The owner's own reply: PENDING_INFO -> IN_REVIEW, closing the newest
    open `info_requests` row, attaching `file_ids` as `application_documents`,
    and shifting `sla_deadline_at` forward by exactly the length of the pause
    (ruling 8) — never re-derived, never left untouched.

    404 `ERR-SYS-003` for a stranger. 409 `ERR-APP-004` in any status but
    PENDING_INFO. 422 `ERR-VAL-001` for a `file_ids` entry that is missing,
    archived or somebody else's upload.
    """
    return ApplicationOut.model_validate(
        await service.respond_info(
            db, application_id, text=payload.text, file_ids=payload.file_ids, actor=actor
        )
    )


# --- Task 5 (3.9b): conclusions and recalculation ------------------------------
#
# `conclusion` takes `Depends(get_current_user)` rather than a fixed
# `require_permission`: which permission it needs depends on the BODY's own
# `kind`, decided per request inside `service.add_conclusion` (a route-level
# dependency is resolved before the body is even parsed, so it cannot see
# `kind` at all). Both branches — `applications.review` for `kind="executor"`,
# `applications.conclude_gis` for `kind="gis"` (fix round 1, task 5) — are
# documented on that function, zone-checked identically either way.
#
# `recalculate` DOES carry a route-level gate, `applications.review` OR
# `.decide` — "the hodim or the head" (ruling 17, narrowed 2026-09-05: the GIS
# specialist is not among them). The WHEN half — which statuses, and whose
# calculation — is `norms.service.save_calculation`'s own guard
# (`_assert_application_open_for_calculation`) and is not repeated here.
#
# Neither carries an `Idempotency-Key`: a repeat conclusion is a second row by
# design (ruling 10), and a repeat recalculation is `calculations`' own
# append-only "the newest wins" (ruling 11) — both replays are the SPECIFIED
# behaviour, not the duplicate the mechanism exists to suppress.


@router.post("/applications/{application_id}/conclusion", status_code=201)
async def add_conclusion(
    application_id: uuid.UUID,
    payload: ApplicationConclusionIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> ApplicationConclusionOut:
    """A specialist's written finding on the application (tz/04 С8) — the
    hodim's `kind="executor"` (`applications.review`) or the GIS specialist's
    `kind="gis"` (`applications.conclude_gis`, see `service.add_conclusion`).
    Immutable: no PATCH, no DELETE anywhere in this module — a repeat
    conclusion after rework is a new row (ruling 10).

    403 `ERR-ACL-001` for a caller who does not hold the permission `kind`
    requires. 404 `ERR-SYS-003` for an id that does not exist or an
    application outside the caller's zone.
    """
    return ApplicationConclusionOut.model_validate(
        await service.add_conclusion(
            db,
            application_id,
            kind=payload.kind,
            text=payload.text,
            recommendation=payload.recommendation,
            actor=actor,
        )
    )


@router.post("/applications/{application_id}/recalculate")
async def recalculate_application(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[
        User, Depends(require_any_permission(APPLICATIONS_REVIEW, APPLICATIONS_DECIDE))
    ],
) -> ApplicationCalculationOut:
    """A new `calculations` row, priced off the application's current stored
    fields against whatever `norms` reads as effective right now — for the
    hodim or the head to call during review (ruling 17; tz/04 С5: after the
    vet/cadastre checks, confirm the price or send it for recalculation).

    409 `ERR-NORM-005` (`norms`' own state-conflict code, never
    `ERR-APP-004`) once the application is APPROVED or beyond — by then the
    figure has been billed and, once a permit exists, printed on a signed
    document.
    """
    return ApplicationCalculationOut.build(
        await service.recalculate(db, application_id, actor=actor)
    )


# --- Task 7: the head's decision -----------------------------------------------
#
# Both routes carry `require_permission(APPLICATIONS_DECIDE)` — the code
# migration 0015 grants `executor_head` («Ваколатли шахс», the leshoz head) and
# 0016 revoked from `leadership` (decision #59). The ZONE is the other half of
# the rule and lives in the service, where the application's own organization
# can be resolved: a permission answers "may this role at all", a zone answers
# "on whose rows", and a staff path needs BOTH (lesson).
#
# No `Idempotency-Key` on either, and for the same reason task 6's two POSTs
# carry none: the mechanism belongs where a replay would allocate something
# scarce. A replayed approve or reject finds the application no longer
# IN_REVIEW and answers 409 (`APPLICATION_TRANSITIONS` has no self-loop), and
# the invoice 3.10a raises off the approval is idempotent by construction on its
# own side.
#
# **A replayed FORWARD is the one case the transition table cannot answer**, and
# the zone does NOT answer it either — an earlier draft of this comment claimed
# it did, and was wrong for exactly the actor a forward exists for.
# `service._assert_in_actor_zone` returns immediately for an actor whose `Zone`
# is empty on all three axes, so an agency- or republic-level head is
# unrestricted nationwide: the first forward moving the application into the
# parent's zone stops a LESHOZ-scoped head from clicking again, and stops nobody
# else. What answers it is `decision._forward`'s own guard — an application
# nobody has taken into work at its current level cannot be escalated from it
# (`assigned_user_id IS NULL` after a forward) — which refuses the second click
# with a 409 instead of walking one rung up the ladder per click. See that
# function's docstring for why the guard is the right shape and not merely a
# replay check.


@router.post("/applications/{application_id}/approve")
async def approve_application(
    application_id: uuid.UUID,
    payload: ApplicationApproveIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_DECIDE))],
) -> ApplicationDecisionOut:
    """The head approves — or, beyond their role's limit, forwards.

    **The answer is never `APPROVED`.** 3.10a's invoice handler subscribes to
    `application_approved` and runs inside this request's own transaction
    (ruling 3а), so a real approval comes back `INVOICED`; APPROVED exists only
    in `application_status_history`. An over-limit request comes back 200 with
    `status: "IN_REVIEW"` and `forwarded_to_organization` set — ruling 9а: the
    application genuinely has not been decided, nothing was signed, and 3.10a
    must not invoice it.

    404 `ERR-SYS-003` for an id that does not exist and for an application
    outside the caller's zone — the same answer to both, since anything else
    makes this route an application-existence oracle; the territorial refusal is
    recorded as RI-12 before it answers. **TWO 409 `ERR-APP-004`s, and the
    second is not about the status**: `reason="bad_transition"` in any status
    but IN_REVIEW, and `reason="not_claimed_at_this_level"` when an over-limit
    application is escalated a second time from a level nobody has taken it
    into work at (`decision._forward`'s replay guard — a head clicking twice
    would otherwise walk one rung up the ladder per click). 422 `ERR-VAL-001`
    when the application carries no stored calculation, when
    `requested_area_ha` is unknown while the role caps area, and when an
    over-limit application sits at an organization with no parent to escalate
    to. 422 `ERR-SIGN-001` for an ERI that does not verify against the
    package bytes.
    """
    application, forwarded_to = await service_decision.approve(
        db,
        application_id,
        pkcs7=payload.pkcs7,
        actor=actor,
        ip=request.client.host if request.client else None,
    )
    return ApplicationDecisionOut.build(application, forwarded_to_organization=forwarded_to)


@router.post("/applications/{application_id}/reject")
async def reject_application(
    application_id: uuid.UUID,
    payload: ApplicationRejectIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_DECIDE))],
) -> ApplicationDecisionOut:
    """IN_REVIEW -> REJECTED, with the grounds `tz/04` С8 requires.

    `reason_item_id` is a REQUIRED field of the body, so a refusal naming no
    reason at all is 422 `ERR-VAL-001` from pydantic — before the handler, and
    therefore before a signature could be spent on a request that cannot
    succeed. A `reason_item_id` outside the `rejection_reasons` classifier, or
    archived, is the service's own 422 `ERR-VAL-001` (`unknown_rejection_
    reason`), still ahead of the ERI.

    `legal_basis` is OPTIONAL at the wire (ruling #182): omitted while the
    application's own benefit claim is `rejected`, the leshoz's own reason
    for THAT becomes the grounds for this; omitted otherwise, still 422
    `ERR-VAL-001` (`legal_basis_required`) — the mandatory-grounds rule
    intact, just enforced one layer in.

    No role limit: decision #29 caps what a head may GRANT. 404 and 409 exactly
    as on `/approve` above.
    """
    return ApplicationDecisionOut.build(
        await service_decision.reject(
            db,
            application_id,
            pkcs7=payload.pkcs7,
            reason_item_id=payload.reason_item_id,
            legal_basis=payload.legal_basis,
            actor=actor,
            ip=request.client.host if request.client else None,
        ),
        forwarded_to_organization=None,
    )


# --- Task 7 (3.9b): external checks — veterinary and cadastre -----------------
#
# Both routes carry `applications.review` alone — maker and confirmer are the
# SAME role (tz/04 С5, the hodim), so unlike a maker/checker split across two
# different codes there is only one to gate the route on; `service.
# confirm_check`'s own identity comparison is what tells the two calls apart
# (lesson: "A maker-checker route needs BOTH roles' permission" — here both
# roles are the same one).


@router.post("/applications/{application_id}/checks", status_code=201)
async def add_application_check(
    application_id: uuid.UUID,
    payload: ApplicationCheckIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_REVIEW))],
) -> ApplicationCheckOut:
    """Either `{check_type}` alone (calls the live vet/cadastre adapter) or
    the paper fallback (`source="manual_fallback"`, `result`, `doc_file_id`) —
    `service.add_check` tells them apart. A paper result is written with
    `confirmed_by=None`; it is not usable until a DIFFERENT reviewer confirms
    it through `POST .../checks/{id}/confirm` below (Oybek's ruling,
    2026-09-05: the paper fallback is exactly the case a second pair of eyes
    exists for).

    404 `ERR-SYS-003` for an id that does not exist or an application outside
    the caller's zone. 422 `ERR-VAL-001` for the paper shape missing `result`
    or `doc_file_id`, or naming a `doc_file_id` that is missing or archived.
    """
    return ApplicationCheckOut.model_validate(
        await service.add_check(db, application_id, payload, actor=actor)
    )


@router.post("/applications/{application_id}/checks/{check_id}/confirm")
async def confirm_application_check(
    application_id: uuid.UUID,
    check_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_REVIEW))],
) -> ApplicationCheckOut:
    """The second person a paper result needs. 409 `ERR-APP-004` refuses the
    MAKER of the same row (`reason="maker_cannot_confirm_own_record"`), a row
    that is not `source="manual_fallback"`, and one already confirmed.

    404 `ERR-SYS-003` for an id that does not exist, an application outside
    the caller's zone, or a `check_id` that does not belong to it.
    """
    return ApplicationCheckOut.model_validate(
        await service.confirm_check(db, application_id, check_id, actor=actor)
    )
