"""`POST /calculations/preview` and `POST /calculations` — the rule engine
behind an endpoint. The two share ONE `service._compute`, so they can never
drift apart, and differ only in what a BLOCKING check does: a preview reports
it as data (`checks`, in the response body — design/03: "ERR-NORM-001..003
inside `checks`, not as an HTTP error", since an applicant must be able to
SEE that a herd is too large, not just be refused); a save refuses with the
mapped ERR-NORM-00x (`service.save_calculation`, via `checks.first_blocking_error`).
A broken INPUT (a missing rule parameter, an unknown benefit code) is an HTTP
error either way — `_compute` raises before either route gets a result to
report.

Both write routes require `get_current_user` only, no permission code: an
applicant prices and requests their own permit (`tz/04` С3), the same reason
`applicant_client` (not a staff client) is what `test_preview_api.py` and
`test_calculations_api.py` sign in as."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.schemas import PAGING_MAX, Page
from app.modules.auth.deps import get_current_user
from app.modules.auth.models import User
from app.modules.norms import repo, service
from app.modules.norms.schemas import CalculationIn, CalculationOut

router = APIRouter(tags=["norms"])


@router.post("/calculations/preview")
async def preview_calculation(
    payload: CalculationIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> Any:
    # No `response_model`: the shape is a free-form dict (`checks[].details`
    # varies by check), not one fixed schema — `service.preview` already
    # returns it fully `calculator.jsonable`-safe (Decimal -> str, never a
    # float), so FastAPI's own encoder has nothing left to convert.
    return await service.preview(db, payload=payload, actor=actor)


@router.post("/calculations", response_model=CalculationOut, status_code=201)
async def create_calculation(
    payload: CalculationIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> Any:
    return await service.save_calculation(db, payload=payload, actor=actor)


# Read is open to any authenticated user in THIS stage: there is no
# `applications` table yet to ask "is this actor the owner or a reviewer of
# the application this calculation belongs to". Stage 3.9 must narrow both
# routes below to the application's owner and its reviewers once that
# ownership exists (carried over in the stage plan).
#
# The WRITE path was closed rather than carried over (I4, final review):
# `CalculationIn.application_id` refused a non-null value outright, because a
# route that persists an unvalidated id into an append-only table is a
# permanent fact nobody can correct. **Stage 3.9a (task 5) opened the field and
# landed the guards in the same commit** — but in `service.save_calculation`,
# NOT here: `applications.service.submit` is the other caller of that function
# and would walk straight past anything written in this router. So this route
# stays `get_current_user`-only by design, and an applicant who names somebody
# else's application is told 404 by the service, an application at APPROVED or
# beyond 409 `ERR-NORM-005`. See `CalculationIn.application_id`'s own comment
# and the block above `save_calculation`.


@router.get("/calculations", response_model=Page[CalculationOut])
async def list_calculations(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
    application_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=PAGING_MAX)] = 0,
) -> Any:
    items, total = await repo.list_calculations(
        db, application_id=application_id, limit=limit, offset=offset
    )
    return Page[CalculationOut](
        items=[CalculationOut.model_validate(item) for item in items],
        total=total,
        page=offset // limit + 1,
        page_size=limit,
    )


@router.get("/calculations/{calculation_id}", response_model=CalculationOut)
async def get_calculation(
    calculation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
) -> Any:
    return await service.get_calculation(db, calculation_id)
