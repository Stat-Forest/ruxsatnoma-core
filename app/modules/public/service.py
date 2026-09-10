"""public service — the module's only door for everyone else (design/01 rule 1).

Seven surfaces, each with its own trust boundary:

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
   EXPLICIT whitelist of `system_settings` keys for the landing's footer —
   never the whole store (`login_max_attempts`, `mfa_enabled` and the other
   operational parameters live in the same table and must never leak here).
5. **Rating summary** — `rating_summary` is read-only, anonymous, and
   publishes the national average of citizens' post-issuance ratings ONLY
   once `permits_service.public_rating_histogram`'s total meets
   `OPEN_DATA_K_ANONYMITY` — the same threshold `open_data_stats` already
   reads, not a second constant (#174).
6. **Application status** — `check_application_status` (task 4) answers a
   citizen who never logged in, on the same "no oracle" posture as
   `check_appeal_status`: a `number` that does not exist and one that exists
   but whose `phone` does not match get the identical `{"found": False}`.
   `_APPLICATION_STATUS_INFO` is this module's own status vocabulary — it
   maps `applications.models.APPLICATION_STATUSES`, not a copy of it, so a
   status this dict has never heard of (impossible while the CHECK
   constraint holds, but worth naming) answers `status_label`/`next_step`
   as `None` rather than raising.
7. **Activity seasons** — `public_activity_seasons` (stage 8 fix wave finding
   1, supersedes the R3 half of decision #175) replaces the deleted
   `site_season_windows` settings key with the REAL windows, resolved through
   `norms_service.resolve_effective_windows` — the same function
   `norms.checks._season_check` calls — so this anonymous read can never
   disagree with the check that fires if the applicant ignores it.
"""

import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import numbers, settings_store
from app.core.errors import err
from app.core.schemas import PageParams
from app.core.time import business_today
from app.modules.admin import repo as admin_repo
from app.modules.admin import service as admin_service
from app.modules.applications import service as applications_service
from app.modules.audit import service as audit
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.norms import service as norms_service
from app.modules.permits import service as permits_service
from app.modules.public import repo
from app.modules.public.models import APPEAL_NUMBER_PREFIX, APPEAL_TRANSITIONS, CitizenAppeal
from app.modules.public.schemas import (
    AppealContact,
    PublicActivitySeasonOut,
    RatingSummaryOut,
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
        normalized["phone"] = _digits(phone)
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


# Task 4: every one of `applications.models.APPLICATION_STATUSES`'s thirteen
# values gets a `label` (`LocalizedName`-shaped: `uz_latn` required, decision
# #90) and a `next_step` sentence, both held in `uz_latn` AND `ru` in ONE dict
# so the label and the sentence can never drift out of step with each other.
# `check_application_status` reads only the `uz_latn` half of `next_step` —
# the public site's base language (#90) — but the `ru` half is written anyway,
# the same way every other citizen-facing string in this codebase is: cheap to
# add beside the first, expensive to backfill once only one language exists.
# `tests/modules/public/test_application_check.py` pins that every member of
# `APPLICATION_STATUSES` has an entry here.
_APPLICATION_STATUS_INFO: dict[str, dict[str, dict[str, str]]] = {
    "SUBMITTED": {
        "label": {"uz_latn": "Yuborildi", "ru": "Подана"},
        "next_step": {
            "uz_latn": "Ariza koʻrib chiqish uchun navbatda.",
            "ru": "Заявка в очереди на рассмотрение.",
        },
    },
    "IN_REVIEW": {
        "label": {"uz_latn": "Koʻrib chiqilmoqda", "ru": "На рассмотрении"},
        "next_step": {
            "uz_latn": "Mutaxassis arizangizni koʻrib chiqmoqda.",
            "ru": "Специалист рассматривает вашу заявку.",
        },
    },
    "PENDING_INFO": {
        "label": {
            "uz_latn": "Qoʻshimcha maʼlumot talab qilinadi",
            "ru": "Требуется дополнительная информация",
        },
        "next_step": {
            "uz_latn": "Shaxsiy kabinetga kirib, soʻralgan maʼlumotni taqdim eting.",
            "ru": "Войдите в личный кабинет и предоставьте запрошенные сведения.",
        },
    },
    "RETURNED": {
        "label": {"uz_latn": "Qaytarildi", "ru": "Возвращена"},
        "next_step": {
            "uz_latn": "Arizani tuzatib, qayta yuboring.",
            "ru": "Исправьте заявку и отправьте её повторно.",
        },
    },
    "APPROVED": {
        "label": {"uz_latn": "Maʼqullandi", "ru": "Одобрена"},
        "next_step": {
            "uz_latn": "Toʻlov hisobvaragʻi tayyorlanmoqda.",
            "ru": "Готовится счёт на оплату.",
        },
    },
    "INVOICED": {
        "label": {"uz_latn": "Toʻlov kutilmoqda", "ru": "Ожидает оплаты"},
        "next_step": {
            "uz_latn": "Shaxsiy kabinetdagi hisobvaraq boʻyicha toʻlovni amalga oshiring.",
            "ru": "Оплатите счёт в личном кабинете.",
        },
    },
    "PAID": {
        "label": {"uz_latn": "Toʻlandi", "ru": "Оплачена"},
        "next_step": {
            "uz_latn": "Ruxsatnoma rasmiylashtirilmoqda.",
            "ru": "Разрешение оформляется.",
        },
    },
    "PERMIT_ISSUED": {
        "label": {"uz_latn": "Ruxsatnoma berildi", "ru": "Разрешение выдано"},
        "next_step": {
            "uz_latn": "Ruxsatnomani shaxsiy kabinetdan yuklab oling.",
            "ru": "Скачайте разрешение в личном кабинете.",
        },
    },
    "REJECTED": {
        "label": {"uz_latn": "Rad etildi", "ru": "Отклонена"},
        "next_step": {
            "uz_latn": "Rad etish sababi bilan shaxsiy kabinetda tanishing.",
            "ru": "Ознакомьтесь с причиной отказа в личном кабинете.",
        },
    },
    "CANCELLED": {
        "label": {"uz_latn": "Bekor qilindi", "ru": "Отменена"},
        "next_step": {
            "uz_latn": "Ariza arizachi tomonidan bekor qilingan.",
            "ru": "Заявка отменена заявителем.",
        },
    },
    "EXPIRED_UNPAID": {
        "label": {"uz_latn": "Toʻlov muddati oʻtib ketdi", "ru": "Истёк срок оплаты"},
        "next_step": {
            "uz_latn": "Yangi ariza topshiring.",
            "ru": "Подайте новую заявку.",
        },
    },
    "CLOSED": {
        "label": {"uz_latn": "Yakunlandi", "ru": "Завершена"},
        "next_step": {
            "uz_latn": "Jarayon yakunlandi, hech qanday amal talab qilinmaydi.",
            "ru": "Процесс завершён, действий не требуется.",
        },
    },
    "ARCHIVED": {
        "label": {"uz_latn": "Arxivlandi", "ru": "Архивирована"},
        "next_step": {
            "uz_latn": "Jarayon yakunlandi, hech qanday amal talab qilinmaydi.",
            "ru": "Процесс завершён, действий не требуется.",
        },
    },
}


def _digits(value: str | None) -> str:
    """Strip everything but digits, so `"+998 90 123-45-67"` and
    `"998901234567"` compare equal. Shared by `_normalize_contact` (a
    phone read off `AppealContact`'s dict shape) and `_phone_matches` below
    (a bare query parameter, not that shape) — one normalization, not two
    copies of the same `isdigit()` filter."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _phone_matches(stored: str | None, given: str) -> bool:
    """`bool(stored_digits)` first, the same guard `_contact_matches` uses for
    email: two blanks must never compare equal, or an application with no
    phone on file would match an empty `phone` query parameter."""
    stored_digits = _digits(stored)
    return bool(stored_digits) and stored_digits == _digits(given)


async def check_application_status(db: AsyncSession, *, number: str, phone: str) -> dict[str, Any]:
    """Always 200-shaped (task 4), the same "no oracle" rule
    `check_appeal_status` applies to its own miss: an unknown `number` and one
    that exists but whose stored phone does not match answer identically,
    `{"found": False}` — `AR-YYYY-NNNNNN` is a gapless, walkable space exactly
    like `MR-YYYY-NNNNNN`, so a distinguishable answer would let a caller
    learn which application numbers are real.

    Only `applications_service.public_status_lookup`'s six columns ever reach
    the response — no contour, no calculation, no attachment, no reviewing
    official — and the applicant's own NAME is never selected in the first
    place (unlike `phone`, read only to be compared, never echoed back).
    Routed through `applications.service` rather than a direct table read
    (stage 8 fix wave finding 2): `applications` is not on CLAUDE.md's
    cross-module read whitelist, and its own docstring says so categorically."""
    row = await applications_service.public_status_lookup(db, number=number)
    if row is None or not _phone_matches(row.phone, phone):
        return {"found": False}
    info = _APPLICATION_STATUS_INFO.get(row.status)
    return {
        "found": True,
        "number": row.number,
        "status": row.status,
        "status_label": info["label"] if info else None,
        "activity_type": (
            row.activity_type_name.get("uz_latn") if row.activity_type_name else None
        ),
        "organization": (row.organization_name.get("uz_latn") if row.organization_name else None),
        "next_step": info["next_step"]["uz_latn"] if info else None,
        "submitted_at": row.submitted_at.date() if row.submitted_at else None,
    }


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
    must never appear here by accident.

    Contacts only since the stage 8 fix wave (finding 1): `season_windows`
    used to ride along here as `site_season_windows`'s six hard-coded month
    lists (ruling R3) — deleted along with that key, superseded by
    `public_activity_seasons` below, which reads the REAL windows instead of
    a settings row nobody keeps in sync with them."""

    async def value(key: str) -> str:
        return await settings_store.get_setting(db, key)

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
    return SiteSettingsOut(contacts=contacts, rules_url=await value("site_rules_url"))


async def public_activity_seasons(db: AsyncSession) -> list[PublicActivitySeasonOut]:
    """`GET /public/activity-seasons` (stage 8 fix wave finding 1 — supersedes
    the R3 half of decision #175): the REAL season windows, replacing
    `site_season_windows`'s six hard-coded month lists.

    The anonymous read names no leshoz, so the one dictionary it can consult
    is the Agency's own — the root organization's `activity_seasons` rows,
    the nationwide default the public calendar shows (2026-09-10, Oybek:
    "make the calendar real with demo data" — which it could not be while
    this read resolved `(None, None)` for every activity, whatever anyone
    had configured). Resolved through `norms_service.effective_season`, the
    SAME path the wizard's date picker and `norms.checks._season_check` use,
    never a second copy of ruling #177's precedence. `is_default=True` on
    every row marks exactly what it says: a real leshoz's own row, reached
    through the authenticated `GET /activity-seasons/effective`, overrides
    this for that leshoz.

    No root organization yet, or no row for an activity — reported honestly
    as "nothing configured" (`[]`, `"none"`), never as "open all year"
    (`resolve_effective_windows`'s own docstring)."""
    activity_types = await admin_repo.list_activity_types(db)
    agency = await admin_repo.get_agency(db)
    seasons = []
    for activity_type in activity_types:
        if agency is None:
            windows, source = norms_service.resolve_effective_windows(None, None)
        else:
            resolved = await norms_service.effective_season(
                db,
                activity_type_id=activity_type.id,
                contour_id=None,
                organization_id=agency.id,
            )
            windows, source = resolved["windows"], resolved["season_source"]
        seasons.append(
            PublicActivitySeasonOut(
                activity_type_code=activity_type.code,
                windows=list(windows),
                season_source=source,
                is_default=True,
            )
        )
    return seasons


async def rating_summary(db: AsyncSession) -> RatingSummaryOut:
    """The landing's single national rating number (#174). Below
    `OPEN_DATA_K_ANONYMITY` ratings nationwide, `published` is `False` and
    BOTH `average` and `histogram` come back `None` — an average over a
    handful of ratings published as a national figure is exactly the "hides
    or overstates" defect this project keeps finding, so the front end gets
    nulls to render as "not enough ratings yet", never a rounded-up or
    zeroed number. Reuses `OPEN_DATA_K_ANONYMITY` rather than a second,
    independent threshold constant — the same value `open_data_stats` reads
    for its own per-cell suppression.

    Routed through `permits.service.public_rating_histogram` (stage 8 fix
    wave finding 2) rather than a direct `permits.models.PermitRating` read
    — `permits` is not on CLAUDE.md's cross-module read whitelist, the same
    reason `open_data_stats` above already goes through `permits_service`.
    """
    histogram = await permits_service.public_rating_histogram(db)
    full = {score: histogram.get(score, 0) for score in range(1, 6)}
    count = sum(full.values())
    if count < OPEN_DATA_K_ANONYMITY:
        return RatingSummaryOut(
            published=False,
            average=None,
            count=count,
            histogram=None,
            threshold=OPEN_DATA_K_ANONYMITY,
        )
    total = sum(score * n for score, n in full.items())
    average = (Decimal(total) / Decimal(count)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return RatingSummaryOut(
        published=True,
        average=average,
        count=count,
        histogram=full,
        threshold=OPEN_DATA_K_ANONYMITY,
    )
