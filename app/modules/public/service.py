"""public service — the module's only door for everyone else (design/01 rule 1).

Three surfaces, each with its own trust boundary:

1. **Citizen appeals** (обращения) — `submit_appeal`/`check_appeal_status` are
   reached with NO authentication at all. `subject`/`body`/`answer_text`/
   `contact` are untrusted, public-authored strings and NEVER cross into a
   raised exception's `details`, a log line, or anything other than the
   `citizen_appeals` row itself (ruling R5, `plans/04.6-4.8-public-help.md`).
2. **Open data** — `open_data_layers`/`open_data_layer_features`/`open_data_stats`
   are read-only, anonymous, and carry no personal data by construction: the
   first two delegate to `gis.service`'s own anonymous-safe reads, the third
   aggregates `permits.service`'s counts and suppresses any cell too small to
   publish (`OPEN_DATA_K_ANONYMITY`, ruling R2).
3. **Staff triage** — `list_appeals`/`get_appeal`/`advance_appeal_status`/
   `answer_appeal` require `public.appeals.manage` (checked by the router's
   `require_permission`, not repeated here).
4. **Site settings** — `site_settings` is read-only, anonymous, and serves an
   EXPLICIT whitelist of `system_settings` keys for the landing's footer and
   season calendar — never the whole store (`login_max_attempts`,
   `mfa_enabled` and the other operational parameters live in the same table
   and must never leak here).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import numbers
from app.core.errors import err
from app.core.schemas import PageParams
from app.core.settings_store import get_setting
from app.core.time import business_today
from app.modules.admin import service as admin_service
from app.modules.audit import service as audit
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.permits import service as permits_service
from app.modules.public import repo
from app.modules.public.models import APPEAL_NUMBER_PREFIX, APPEAL_TRANSITIONS, CitizenAppeal
from app.modules.public.schemas import (
    AppealContact,
    SiteContactsOut,
    SiteSettingsOut,
    SiteSocialOut,
    SiteTextOut,
)

# R2 (`plans/04.6-4.8-public-help.md`): a smaller cell reveals a specific
# applicant's business — see the plan for the concrete reasoning. A constant,
# not a `system_settings` row: changing WHAT counts as "too few to publish" is
# a product decision that should leave a code diff, not a silent admin toggle.
OPEN_DATA_K_ANONYMITY = 5


def _normalize_contact(contact: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    phone = contact.get("phone")
    if phone:
        normalized["phone"] = "".join(ch for ch in str(phone) if ch.isdigit())
    email = contact.get("email")
    if email:
        normalized["email"] = str(email).strip().lower()
    return normalized


def _contact_matches(stored: dict[str, Any], given: AppealContact) -> bool:
    stored_norm = _normalize_contact(stored)
    given_norm = _normalize_contact(given.model_dump(exclude_none=True))
    if given_norm.get("phone") and given_norm["phone"] == stored_norm.get("phone"):
        return True
    return bool(given_norm.get("email") and given_norm["email"] == stored_norm.get("email"))


async def submit_appeal(
    db: AsyncSession, *, applicant_name: str, contact: AppealContact, subject: str, body: str
) -> str:
    """Anonymous (С27: "любой, без авторизации"). Returns the number only —
    the caller already knows everything else they submitted."""
    number = await numbers.next_public_number(db, APPEAL_NUMBER_PREFIX, business_today())
    appeal = CitizenAppeal(
        number=number,
        applicant_name=applicant_name,
        contact=contact.model_dump(exclude_none=True),
        subject=subject,
        body=body,
        status="new",
    )
    await repo.add(db, appeal)
    # No `subject`/`body`/`contact` in the audit row (ruling R5) — the number
    # and id are enough to find it again; `user_id=None` matches every other
    # anonymous/system-authored entry in this codebase (outbox jobs, webhooks).
    await audit.log(
        db, action="citizen_appeal.submit", object_type="citizen_appeal", object_id=appeal.id
    )
    return number


async def check_appeal_status(
    db: AsyncSession, *, number: str, contact: AppealContact
) -> dict[str, Any]:
    """Always 200-shaped (R3): an unknown number and a contact mismatch answer
    identically, `{"found": False}` — the same "no oracle" rule
    `permits.public_router`'s QR check applies to its own miss, for the
    identical reason: `MR-YYYY-NNNNNN` is a gapless, walkable space."""
    appeal = await repo.get_by_number(db, number)
    if appeal is None or not _contact_matches(appeal.contact, contact):
        return {"found": False}
    return {
        "found": True,
        "status": appeal.status,
        "subject": appeal.subject,
        "answer_text": appeal.answer_text,
        "answered_at": appeal.answered_at,
    }


async def _appeal_or_404(db: AsyncSession, appeal_id: uuid.UUID) -> CitizenAppeal:
    appeal = await repo.get_by_id(db, appeal_id)
    if appeal is None:
        raise err("ERR-SYS-003")
    return appeal


async def list_appeals(
    db: AsyncSession, *, status: str | None, params: PageParams
) -> tuple[list[CitizenAppeal], int]:
    return await repo.list_appeals(db, status=status, offset=params.offset, limit=params.page_size)


async def get_appeal(db: AsyncSession, appeal_id: uuid.UUID) -> CitizenAppeal:
    return await _appeal_or_404(db, appeal_id)


def _assert_transition(appeal: CitizenAppeal, to_status: str) -> None:
    allowed = APPEAL_TRANSITIONS.get(appeal.status, ())
    if to_status not in allowed:
        raise err("ERR-PUB-001", details={"from_status": appeal.status, "to_status": to_status})


async def advance_appeal_status(
    db: AsyncSession, appeal_id: uuid.UUID, *, to_status: str, actor: User
) -> CitizenAppeal:
    """`in_progress`/`closed` only — `answered` is reached exclusively through
    `answer_appeal`, which is what keeps the `answered_fields_consistent`
    CHECK satisfied on every row that reaches it."""
    appeal = await _appeal_or_404(db, appeal_id)
    _assert_transition(appeal, to_status)
    old_status = appeal.status
    appeal.status = to_status
    await audit.log(
        db,
        action="citizen_appeal.advance",
        user_id=actor.id,
        object_type="citizen_appeal",
        object_id=appeal.id,
        old_value={"status": old_status},
        new_value={"status": to_status},
    )
    return appeal


async def answer_appeal(
    db: AsyncSession, appeal_id: uuid.UUID, *, answer_text: str, actor: User
) -> CitizenAppeal:
    appeal = await _appeal_or_404(db, appeal_id)
    _assert_transition(appeal, "answered")
    appeal.status = "answered"
    appeal.answer_text = answer_text
    appeal.answered_by = actor.id
    appeal.answered_at = datetime.now(UTC)
    # `answer_text` is deliberately absent from the audit row (ruling R5) —
    # the fact that an answer was given, and by whom, is what matters for the
    # trail; the text itself is already the row this action just wrote.
    await audit.log(
        db,
        action="citizen_appeal.answer",
        user_id=actor.id,
        object_type="citizen_appeal",
        object_id=appeal.id,
        new_value={"status": "answered"},
    )
    return appeal


async def open_data_layers(db: AsyncSession) -> list[Any]:
    layers = await gis_service.list_layers(db)
    return [layer for layer in layers if layer.is_public]


async def open_data_layer_features(db: AsyncSession, code: str) -> dict[str, Any]:
    return await gis_service.public_features(db, code)


async def open_data_stats(db: AsyncSession) -> dict[str, Any]:
    """Republic totals are never suppressed; the region/organization breakdown
    drops any cell below `OPEN_DATA_K_ANONYMITY` distinct active permits
    (ruling R2) — omitted entirely, not zeroed, so its absence reads as
    "small", never as a precise small number."""
    org_stats = await permits_service.public_active_stats_by_organization(db)
    organizations = {
        org.id: org for org in await admin_service.list_organizations(db, kind="leshoz")
    }
    regions = {region.id: region for region in await admin_service.list_regions(db)}

    total_count = sum(row["active_count"] for row in org_stats)
    total_area = sum((row["active_area_ha"] for row in org_stats), Decimal("0"))

    by_organization = []
    by_region_agg: dict[uuid.UUID | None, dict[str, Any]] = {}
    for row in org_stats:
        org = organizations.get(row["organization_id"])
        region_id = org.region_id if org is not None else None
        bucket = by_region_agg.setdefault(
            region_id, {"active_permits_count": 0, "active_area_ha": Decimal("0")}
        )
        bucket["active_permits_count"] += row["active_count"]
        bucket["active_area_ha"] += row["active_area_ha"]

        if row["active_count"] < OPEN_DATA_K_ANONYMITY or org is None:
            continue
        region = regions.get(region_id) if region_id is not None else None
        by_organization.append(
            {
                "organization_id": org.id,
                "organization_name": org.name,
                "region_id": region_id,
                "region_name": region.name if region is not None else None,
                "active_permits_count": row["active_count"],
                "active_area_ha": row["active_area_ha"],
            }
        )

    by_region = []
    for region_id, agg in by_region_agg.items():
        if agg["active_permits_count"] < OPEN_DATA_K_ANONYMITY:
            continue
        region = regions.get(region_id) if region_id is not None else None
        by_region.append(
            {
                "region_id": region_id,
                "region_name": region.name if region is not None else None,
                "active_permits_count": agg["active_permits_count"],
                "active_area_ha": agg["active_area_ha"],
            }
        )

    return {
        "k_anonymity_threshold": OPEN_DATA_K_ANONYMITY,
        "total_active_permits": total_count,
        "total_active_area_ha": total_area,
        "by_region": by_region,
        "by_organization": by_organization,
    }


async def site_settings(db: AsyncSession) -> SiteSettingsOut:
    """Exactly the keys the public site needs — never the whole settings
    store. `system_settings` also holds operational parameters
    (`login_max_attempts`, `mfa_enabled`, `session_absolute_hours`, ...); this
    whitelist is the point of the route, so a future key added to the store
    must never appear here by accident."""

    async def value(key: str) -> str:
        return await get_setting(db, key)

    telegram = await value("site_social_telegram")
    youtube = await value("site_social_youtube")
    contacts = SiteContactsOut(
        phone=await value("site_contact_phone"),
        email=await value("site_contact_email"),
        address=SiteTextOut(
            uz_latn=await value("site_contact_address_uz"),
            ru=await value("site_contact_address_ru"),
        ),
        hours=SiteTextOut(
            uz_latn=await value("site_contact_hours_uz"),
            ru=await value("site_contact_hours_ru"),
        ),
        social=SiteSocialOut(telegram=telegram or None, youtube=youtube or None),
    )
    return SiteSettingsOut(
        contacts=contacts,
        season_windows=await get_setting(db, "site_season_windows"),
    )
