"""Certificates a caller may list/bind/unbind, one object's signature list,
and oversight re-verification (Task 7).

Two independent resource families in one router — `router = APIRouter(tags=
[...])` with NO prefix of its own, the same shape `norms/calc_router.py` and
`norms/router.py` use, since neither `/certificates` nor `/signatures` is a
sub-path of the other. Mounted once in `app.main` with `prefix="/api/v1"`,
producing `/api/v1/certificates` and `/api/v1/signatures` from one
`include_router` call (plan ruling 3, approved by the customer): the original
contract draft put these under `/auth/certificates` back when the tables were
expected to live in `auth` — they moved here WITH the tables, into this
module's own router, never mounted under another module's prefix."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.signatures import service
from app.modules.signatures.permissions import REVERIFY
from app.modules.signatures.schemas import CertificateBindIn, CertificateOut, SignatureOut

router = APIRouter(tags=["signatures"])


@router.get("/certificates", response_model=Page[CertificateOut])
async def list_certificates(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    params: Annotated[PageParams, Depends()],
) -> Page[CertificateOut]:
    """The caller's own BOUND certificates only (`unbound_at IS NULL`) —
    unbinding (`DELETE` below) never deletes a row, it only takes it off this
    list. Paged from its first commit, the same `Page[T]`/`PageParams`
    envelope every other list route in this app uses (lesson: page a list
    from the first commit, not after the fact)."""
    items, total = await service.list_my_certificates(db, user=user, params=params)
    return Page[CertificateOut](
        items=[CertificateOut.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.post("/certificates", status_code=201)
async def create_certificate(
    payload: CertificateBindIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> CertificateOut:
    """Bind a certificate ahead of any actual signing, from a signed
    challenge — ownership proven by PINFL/STIR the same way `sign()` proves
    it on every call (ruling 4)."""
    cert = await service.register_certificate(
        db,
        pkcs7=payload.pkcs7,
        user=user,
        ip=request.client.host if request.client else None,
    )
    return CertificateOut.model_validate(cert)


@router.delete("/certificates/{certificate_id}", status_code=204)
async def delete_certificate(
    certificate_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Unbind, never delete (pre-flight ruling P3): sets `unbound_at`, leaves
    `status` — the certificate's own PKI state — untouched. The owner only; a
    certificate a signature references must survive forever."""
    await service.unbind_certificate(db, certificate_id=certificate_id, user=user)


@router.get("/signatures", response_model=Page[SignatureOut])
async def list_signatures(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    params: Annotated[PageParams, Depends()],
    object_type: str,
    object_id: uuid.UUID,
) -> Page[SignatureOut]:
    """The object's owner (anyone holding at least one signature row of
    their own against it) or `signatures.view_any` (oversight) — a check
    INSIDE the service, not a `require_permission` dependency here, which
    would reject the object's own signer before the service ever got a
    chance to say otherwise (lesson: a permission check alone is not enough
    on a read path that also needs an ownership check). Ordered by
    `(signed_at, id)` and paged from its first commit (lessons)."""
    items, total = await service.list_signatures_page(
        db, object_type=object_type, object_id=object_id, user=user, params=params
    )
    return Page[SignatureOut](
        items=[SignatureOut.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.post("/signatures/{signature_id}/reverify", response_model=SignatureOut)
async def reverify_signature(
    signature_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REVERIFY))],
) -> SignatureOut:
    """Oversight re-check (ruling 5): re-derives the certificate's CURRENT
    standing and writes a brand new record — the original is evidence of
    what was true at signing time and is never rewritten. 200, not 201: this
    reports the result of re-checking an existing signature, not a resource
    with a URL of its own a client would GET again."""
    new_row = await service.reverify(db, signature_id=signature_id, user=user)
    return SignatureOut.model_validate(new_row)
