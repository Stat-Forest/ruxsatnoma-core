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
    ApplicationCreate,
    ApplicationOut,
    ApplicationPatch,
    ApplicationStatus,
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
