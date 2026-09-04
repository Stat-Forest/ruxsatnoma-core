"""Permit routes.

`POST /applications/{id}/permit` lives on the applications path because
`design/03` puts it there and a front-end already reads that contract — but the
MODULE is `permits`, and this is its router. A path is not a module boundary:
`applications` is level 3 and may not ask whether a permit exists, so the route
that creates one cannot live in its router.

No `Idempotency-Key` on this POST, deliberately (`app/core/idempotency.py` is the
mechanism 3.6a's import route uses). `permits.application_id` is unique and the
service refuses a second issuance with `ERR-PERM-001`, so a replayed request is
answered as a domain conflict rather than producing a second numbered document —
which is a stronger guarantee than a replay window, and one that also holds for a
second request that is not a replay at all.

Importing `permissions` below is what registers this module's four codes (the
same idiom every other router uses). Migration 0019 grants them to four roles, and
`tests/test_permissions_registry.py` fails on a granted code the registry never
learned — which is why `app/main.py` carried a stand-in import until this file
existed.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files
from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.permits import service
from app.modules.permits.permissions import PERMITS_ISSUE, PERMITS_SIGN
from app.modules.permits.schemas import (
    PermitCardOut,
    PermitOut,
    PermitSignatureOut,
    PermitSignIn,
    PermitStatus,
)

router = APIRouter(tags=["permits"])


@router.post("/applications/{application_id}/permit", status_code=201)
async def issue_permit(
    application_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PERMITS_ISSUE))],
) -> PermitOut:
    """Form the permit for a PAID application: 201 with the numbered document
    awaiting its four signatures.

    422 `ERR-PAY-001` when the application is not PAID — and that refusal is
    recorded as RI-10 (`tz/10`: CRITICAL, immediate) before the exception that
    explains it. 409 `ERR-PERM-001` when this application already has a permit.
    403 `ERR-ACL-002` when the plot is outside the caller's zone, which the
    service checks in addition to the permission gate above.
    """
    permit = await service.issue(db, application_id, actor=actor)
    return PermitOut.model_validate(permit)


@router.post("/permits/{permit_id}/signatures")
async def sign_permit(
    permit_id: uuid.UUID,
    payload: PermitSignIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(PERMITS_SIGN))],
) -> PermitSignatureOut:
    """Attach one of the permit's four ERI signatures (C11). 200 with the
    permit's status and whatever is still missing; once nothing is missing the
    permit is ACTIVE, `issued_at` is stamped and the application has moved to
    PERMIT_ISSUED (ruling 18).

    200, not 201: the client does not get a URL for the signature it just made —
    `GET /signatures?object_type=permit&object_id=…` (3.8's own route) lists
    them, and what this answers is the state of the PERMIT.

    `permits.sign` is held by all four signatory roles (migration 0019), so this
    dependency only answers "may this role sign anything at all". WHICH of the
    four lines this particular actor may sign is `signers.py`'s map, checked in
    the service before the signature is taken — 403 `ERR-ACL-001` with
    `details.reason = "signer_not_authorized"` (lesson: a permission answers "at
    all", a second check answers "on what"). 409 `ERR-PERM-001` when the permit
    is not awaiting signatures; 422 `ERR-SIGN-001` when the envelope does not
    verify, in which case the attempt is still stored as evidence (3.8 ruling 8).

    No `Idempotency-Key`, for the same reason issuance needs none: a replay is
    refused by `uq_signatures_valid_purpose`, which `sign()` reports as 409
    `ERR-SIGN-002` — a permanent guarantee rather than one bounded by a replay
    window.
    """
    permit = await service.add_signature(
        db, permit_id, purpose=payload.purpose, pkcs7=payload.pkcs7, user=actor
    )
    # `model_validate`, not the constructor: `permits.status` is a plain `str`
    # column and `PermitStatus` is a `Literal`, so pydantic checks the membership
    # at runtime here rather than the call site suppressing a type error — the
    # same reason `PermitOut` is built by validation and not by hand.
    return PermitSignatureOut.model_validate(
        {
            "status": permit.status,
            "missing_signatures": await service.missing_signatures(db, permit.id),
        }
    )


# --- Task 8: the read surface ------------------------------------------------
#
# `Depends(get_current_user)` and NOT `require_permission(PERMITS_VIEW_ANY)` on
# any of the three: each route also admits the permit's own HOLDER, who holds no
# such grant, so a route-level dependency would reject a citizen reading their
# own permit before the ownership check ever ran. The permission and the zone are
# both applied inside the service (`_readable_permit` / `list_permits`) — a
# permission answers "may this role at all", a zone answers "on whose rows", and
# a read path needs both (lesson). `GET /signatures` is gated exactly this way,
# for exactly this reason.


@router.get("/permits")
async def list_permits(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    params: Annotated[PageParams, Depends()],
    status: PermitStatus | None = None,
    applicant_id: uuid.UUID | None = None,
    contour_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    series: Annotated[str | None, Query(max_length=service.SERIES_MAX_LENGTH)] = None,
    number: Annotated[int | None, Query(ge=1, le=service.MAX_PERMIT_NUMBER)] = None,
) -> Page[PermitOut]:
    """The permits this caller may see: their own, or — holding
    `permits.view_any` — their zone's.

    A caller entitled to nothing gets an empty page, never a 403: a list has no
    row to refuse. The card next door is where a territorial refusal is spelled
    out (`ERR-ACL-002`), because there the caller has named one permit.

    `status` is the `PermitStatus` literal, so a typo is a 422 rather than an
    empty page that reads as "no permits in that state". `number` carries an
    upper bound because it is bound into SQL as a bigint and an unbounded one
    reaches asyncpg as `DataError: value out of int64 range` — a 500 for a query
    string anybody can type (lesson), enforced by
    `tests/test_code_conventions.py::test_every_integer_query_parameter_carries_an_upper_bound`.
    """
    items, total = await service.list_permits(
        db,
        actor=user,
        params=params,
        status=status,
        applicant_id=applicant_id,
        contour_id=contour_id,
        organization_id=organization_id,
        series=series,
        number=number,
    )
    return Page[PermitOut](
        items=[PermitOut.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/permits/{permit_id}")
async def get_permit_card(
    permit_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> PermitCardOut:
    """The permit, its four signature lines and its timeline.

    404 `ERR-SYS-003` both for an id that does not exist and for a permit this
    caller has no claim on — the same answer on purpose, since two different
    answers make the route a permit-existence oracle. 403 `ERR-ACL-002` only for
    a `permits.view_any` holder outside the permit's zone, who already knows
    permits exist and is owed the territorial reason.
    """
    return PermitCardOut.build(await service.permit_card(db, permit_id, actor=user))


@router.get("/permits/{permit_id}/pdf")
async def download_permit_pdf(
    permit_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> Response:
    """The stored PDF/A — the exact bytes `doc_hash` was frozen over and all four
    ERI signatures cover, never a re-render (ruling 3).

    Not `GET /files/{id}` with the permit's `pdf_file_id`: the file subsystem's
    access rule is "the uploader, or any non-applicant role", and the uploader is
    the issuing hodim — so that route would refuse the permit's own holder their
    own permit. The access rule here is the permit's.

    `inline`, unlike `/files/{id}`'s default: a citizen opening their permit
    wants to look at it, and unlike an uploaded file these bytes are ours —
    rendered by `permits.render`, never supplied by a caller. `nosniff` rides
    along anyway.

    The filename is RFC 6266/5987-encoded through the one shared helper
    (`core.files.content_disposition`): `permits.series` is the CYRILLIC «А»,
    which Starlette cannot latin-1 encode, so a raw interpolation would 500 every
    download (lesson).
    """
    permit, data = await service.permit_document(db, permit_id, actor=user)
    filename = f"permit-{permit.series}-{permit.number:06d}.pdf"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": files.content_disposition("inline", filename),
            "X-Content-Type-Options": "nosniff",
        },
    )
