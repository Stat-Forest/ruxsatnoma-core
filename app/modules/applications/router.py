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

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
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
    PrecheckCalculationOut,
    PrecheckOut,
)
from app.modules.auth.deps import get_current_user, require_permission
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
