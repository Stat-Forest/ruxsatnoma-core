"""The anonymous surface — appeals, open data, public site settings, the
national rating summary, application status and the real activity seasons.
No `get_current_user`, no permission code anywhere in this file; a rate limit
instead of both, same idiom `permits.public_router` and `norms.public_router`
already established.

`POST /public/appeals`, `GET /public/appeals/check` and `GET
/public/applications/check` are in `app/core/logging.py`'s
`SILENT_ACCESS_LOG_PATHS` — the latter two's `contact`/`phone` query
parameter is exactly the kind of thing that precedent exists to keep out of a
process log. The open-data, site-settings, ratings-summary and
activity-seasons routes carry no personal data and are deliberately left out
of that list."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.ratelimit import rate_limit
from app.modules.public import service
from app.modules.public.schemas import (
    AppealContact,
    AppealIn,
    AppealStatusOut,
    AppealSubmitOut,
    ApplicationStatusOut,
    OpenDataLayerOut,
    OpenDataStatsOut,
    PublicActivitySeasonOut,
    RatingSummaryOut,
    SiteSettingsOut,
)

router = APIRouter(prefix="/public", tags=["public"])

_APPEAL_SUBMIT_LIMIT = Depends(
    rate_limit("public_appeal_submit", "ratelimit_public_appeal_submit_per_minute")
)
_APPEAL_STATUS_LIMIT = Depends(
    rate_limit("public_appeal_status", "ratelimit_public_appeal_status_per_minute")
)
_OPEN_DATA_LIMIT = Depends(rate_limit("public_open_data", "ratelimit_public_open_data_per_minute"))


@router.post(
    "/appeals", response_model=AppealSubmitOut, status_code=201, dependencies=[_APPEAL_SUBMIT_LIMIT]
)
async def submit_appeal(payload: AppealIn, db: Annotated[AsyncSession, Depends(get_db)]) -> Any:
    number = await service.submit_appeal(
        db,
        applicant_name=payload.applicant_name,
        contact=payload.contact,
        subject=payload.subject,
        body=payload.body,
    )
    return {"number": number}


@router.get("/appeals/check", response_model=AppealStatusOut, dependencies=[_APPEAL_STATUS_LIMIT])
async def check_appeal_status(
    number: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    phone: str | None = None,
    email: str | None = None,
) -> Any:
    """`phone`/`email` — the shared secret R3 requires (`plans/
    04.6-4.8-public-help.md`). Neither is validated as a real phone/email
    shape here: an unparsable value simply never matches anything, the same
    "found: false" answer an unknown number gets — validating it would only
    buy an attacker a way to distinguish "malformed" from "wrong" for free."""
    contact = AppealContact.model_construct(phone=phone, email=email)
    return await service.check_appeal_status(db, number=number, contact=contact)


@router.get(
    "/applications/check",
    response_model=ApplicationStatusOut,
    dependencies=[_APPEAL_STATUS_LIMIT],
)
async def check_application_status(
    number: str, phone: str, db: Annotated[AsyncSession, Depends(get_db)]
) -> Any:
    """Task 4: status without logging in. `phone` is compared against the
    applicant's own contact on file (`service.check_application_status`'s own
    docstring) — not validated as a real phone shape here, for the identical
    reason `check_appeal_status` above does not validate its own
    `phone`/`email`: an unparsable value simply never matches anything, the
    same `found: false` an unknown number gets. Shares `_APPEAL_STATUS_LIMIT`'s
    bucket rather than a new settings key — both are the same shape of
    low-volume, anonymous "check my status" call."""
    return await service.check_application_status(db, number=number, phone=phone)


@router.get(
    "/open-data/layers", response_model=list[OpenDataLayerOut], dependencies=[_OPEN_DATA_LIMIT]
)
async def open_data_layers(db: Annotated[AsyncSession, Depends(get_db)]) -> Any:
    return await service.open_data_layers(db)


@router.get("/open-data/layers/{code}/features", dependencies=[_OPEN_DATA_LIMIT])
async def open_data_layer_features(
    code: str, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict[str, Any]:
    return await service.open_data_layer_features(db, code)


@router.get("/open-data/stats", response_model=OpenDataStatsOut, dependencies=[_OPEN_DATA_LIMIT])
async def open_data_stats(db: Annotated[AsyncSession, Depends(get_db)]) -> Any:
    return await service.open_data_stats(db)


@router.get("/site-settings", response_model=SiteSettingsOut, dependencies=[_OPEN_DATA_LIMIT])
async def site_settings(db: Annotated[AsyncSession, Depends(get_db)]) -> Any:
    """Feeds the landing footer — an explicit whitelist, never a proxy of
    `system_settings` (`service.site_settings`)."""
    return await service.site_settings(db)


@router.get(
    "/activity-seasons",
    response_model=list[PublicActivitySeasonOut],
    dependencies=[_OPEN_DATA_LIMIT],
)
async def public_activity_seasons(db: Annotated[AsyncSession, Depends(get_db)]) -> Any:
    """The REAL season windows (stage 8 fix wave finding 1) — replaces the
    deleted `site_season_windows` settings key. Resolved through the SAME
    function the blocking check itself calls
    (`norms.checks.resolve_effective_windows`); see `service.
    public_activity_seasons` for what `is_default` means and whose rows
    (the Agency's) the anonymous read shows. Shares `_OPEN_DATA_LIMIT`'s bucket rather than a new
    settings key — the same low-volume, cacheable-read shape as
    `/site-settings` and `/ratings/summary` beside it."""
    return await service.public_activity_seasons(db)


@router.get("/ratings/summary", response_model=RatingSummaryOut, dependencies=[_OPEN_DATA_LIMIT])
async def rating_summary(db: Annotated[AsyncSession, Depends(get_db)]) -> Any:
    """The national citizen-rating average — suppressed below the threshold
    (#174, `service.rating_summary`): `average`/`histogram` are `null` and
    `published` is `false` until enough citizens have rated a permit."""
    return await service.rating_summary(db)
