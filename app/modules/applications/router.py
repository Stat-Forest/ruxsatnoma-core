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

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.idempotency import IdempotencyContext
from app.core.schemas import Page, PageParams
from app.modules.applications import service
from app.modules.applications.permissions import APPLICATIONS_CREATE
from app.modules.applications.schemas import (
    ApplicationCardOut,
    ApplicationCheckOut,
    ApplicationCreate,
    ApplicationDocumentIn,
    ApplicationDocumentOut,
    ApplicationOut,
    ApplicationPatch,
    ApplicationStatus,
    ApplicationSubmitIn,
    PrecheckCalculationOut,
    PrecheckOut,
)
from app.modules.auth.deps import get_current_user, idempotency_context, require_permission
from app.modules.auth.models import User

# `applications.number` is `RX-<yyyy>-<seq>` (plan ruling 5а). Bounded because
# it is bound into SQL as text from a query string anybody can type; the value
# itself is matched exactly, never as a pattern.
NUMBER_MAX_LENGTH = 64

router = APIRouter(tags=["applications"])


@router.post("/applications", status_code=201)
async def create_application(
    payload: ApplicationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(APPLICATIONS_CREATE))],
) -> ApplicationOut:
    """201 with an empty DRAFT: a draft is autosaved field by field (ruling 7),
    so everything except who is filing and for whom arrives through PATCH.

    422 `ERR-VAL-001` when the caller has no `applicants` row of their own
    (`on_behalf="self"`) or names an applicant that is not theirs; 403
    `ERR-ACL-001` when `on_behalf="legal"` names a legal entity the caller holds
    no effective representation of.
    """
    return ApplicationOut.model_validate(await service.create_draft(db, payload, actor=actor))


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
        checks=[ApplicationCheckOut.model_validate(row) for row in card["checks"]],
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

    400 `ERR-APP-001` (missing fields, NAMED); 409 `ERR-APP-004` in any status
    but DRAFT; 422 `ERR-APP-003` for a benefit claim with no supporting
    document; 409 `ERR-GIS-005` for a contour with no published version;
    `ERR-GIS-001/002/005` or `ERR-NORM-001/002/003/006` when a BLOCKING check
    fails — the difference from the pre-check, which reports the identical
    result as data; 422 `ERR-SIGN-001` for an invalid signature; 409
    `ERR-APP-002` with the existing number when another active application
    already covers this plot and period.
    """
    application = await service.submit(db, application_id, pkcs7=payload.pkcs7, actor=actor)
    out = ApplicationOut.model_validate(application)
    await ctx.save(db, status_code=200, body=out.model_dump(mode="json"))
    return out
