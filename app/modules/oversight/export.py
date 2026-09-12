"""`GET /oversight/risk-indicators/export.xlsx` and `GET /oversight/events/
export.xlsx` (stage 13, ruling #204): each list on paper.

Both `rows_*` functions call the module's own `service.list_risk_indicators`/
`service.list_events` — the same zone scope and the same per-view audit row
С22 requires (ruling R2: the export never widens what the screen shows, and
never adds a second audit entry beside the one the service already writes on
every call). `object_id` is exported as the raw UUID text: resolving it to
the referenced row's own human number would need one batch query PER OBJECT
TYPE (`application`, `permit`, `invoice`, `report`, `inspection_act`, …) and
is deferred to a later stage rather than guessed here.
"""

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.oversight import service
from app.modules.oversight.models import OversightEvent, RiskIndicator

# Copied verbatim from the adminka (`src/i18n/uz_latn.ts` / `ru.ts`,
# `leadership.oversight.level.*` / `.status.*` / `.object.*` / `.rnStatus.*`,
# and `src/pages/oversight/format.ts::OBJECT_TYPE_LABEL_KEYS`) so the file
# reads like the screen. An unknown code renders as itself (`_label` below).
LEVEL_LABELS: dict[str, dict[str, str]] = {
    "low": {"uz_latn": "past", "ru": "низкий"},
    "medium": {"uz_latn": "oʻrta", "ru": "средний"},
    "high": {"uz_latn": "yuqori", "ru": "высокий"},
    "critical": {"uz_latn": "kritik", "ru": "критический"},
}
STATUS_LABELS: dict[str, dict[str, str]] = {
    "new": {"uz_latn": "Yangi", "ru": "Новый"},
    "in_review": {"uz_latn": "Koʻrib chiqilmoqda", "ru": "На рассмотрении"},
    "closed": {"uz_latn": "Yopilgan", "ru": "Закрыт"},
}
OBJECT_TYPE_LABELS: dict[str, dict[str, str]] = {
    "application": {"uz_latn": "Ariza", "ru": "Заявка"},
    "permit": {"uz_latn": "Ruxsatnoma", "ru": "Разрешение"},
    "invoice": {"uz_latn": "Hisob-faktura", "ru": "Счет-фактура"},
    "report": {"uz_latn": "Hisobot", "ru": "Отчет"},
    "act": {"uz_latn": "Dalolatnoma", "ru": "Акт"},
    "inspection_act": {"uz_latn": "Dalolatnoma", "ru": "Акт"},
}
RN_STATUS_LABELS: dict[str, dict[str, str]] = {
    "internal": {"uz_latn": "Ichki", "ru": "Внутренний"},
    "pending": {"uz_latn": "Yuborishga navbatda", "ru": "В очереди на отправку"},
    "sent": {"uz_latn": "Yuborilgan", "ru": "Отправлен"},
    "failed": {"uz_latn": "Yuborishda xatolik", "ru": "Ошибка отправки"},
}
TITLE_RISK_INDICATORS = {"uz_latn": "Xavf koʻrsatkichlari", "ru": "Индикаторы риска"}
TITLE_EVENTS = {"uz_latn": "Hodisalar", "ru": "События"}


def _label(table: dict[str, dict[str, str]], code: str | None, lang: xlsx.Lang) -> str:
    """An unknown or missing code renders as itself (or empty) — never a
    blank cell that hides which code the row actually carried."""
    if code is None:
        return ""
    return table.get(code, {}).get(lang, code)


class RiskIndicatorRow:
    """One `risk_indicators` row plus the responsible user's name, resolved
    in ONE batch query (`rows_risk_indicators`) rather than per row."""

    def __init__(self, item: RiskIndicator, *, responsible: str) -> None:
        self.item = item
        self.id = item.id
        self.responsible = responsible


def columns_risk_indicators(lang: xlsx.Lang) -> list[xlsx.Column[RiskIndicatorRow]]:
    a = lambda f: lambda r: getattr(r.item, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column("code", {"uz_latn": "Kod", "ru": "Код"}, a("code"), 10),
        xlsx.Column(
            "level",
            {"uz_latn": "Daraja", "ru": "Уровень"},
            lambda r: _label(LEVEL_LABELS, r.item.level, lang),
            14,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(STATUS_LABELS, r.item.status, lang),
            20,
        ),
        xlsx.Column(
            "object_type",
            {"uz_latn": "Obyekt turi", "ru": "Тип объекта"},
            lambda r: _label(OBJECT_TYPE_LABELS, r.item.object_type, lang),
            18,
        ),
        xlsx.Column(
            "object_id",
            {"uz_latn": "Obyekt", "ru": "Объект"},
            lambda r: str(r.item.object_id) if r.item.object_id else "",
            38,
        ),
        xlsx.Column("description", {"uz_latn": "Tavsif", "ru": "Описание"}, a("description"), 44),
        xlsx.Column(
            "responsible", {"uz_latn": "Masʼul", "ru": "Ответственный"}, lambda r: r.responsible, 24
        ),
        xlsx.Column("occurred_at", {"uz_latn": "Vaqt", "ru": "Время"}, a("occurred_at"), 18),
        xlsx.Column(
            "rn_status",
            {"uz_latn": "RN holati", "ru": "Статус RN"},
            lambda r: _label(RN_STATUS_LABELS, r.item.rn_status, lang),
            16,
        ),
        xlsx.id_column(),
    ]


async def rows_risk_indicators(
    db: AsyncSession,
    *,
    actor: User,
    lang: xlsx.Lang,
    code: str | None,
    level: str | None,
    status: str | None,
    object_type: str | None,
    object_id: uuid.UUID | None,
    period_from: date | None,
    period_to: date | None,
) -> tuple[list[RiskIndicatorRow], int, int]:
    """(rows, total, cap). `model_construct` bypasses `PageParams`'s own
    `page_size <= 100` — the export is the one caller legitimately above it,
    the cap is what bounds it instead (ruling R3). `service.list_risk_
    indicators` already audits this call (С22) — no second audit row here."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    items, total = await service.list_risk_indicators(
        db,
        actor=actor,
        params=PageParams.model_construct(page=1, page_size=cap),
        code=code,
        level=level,
        status=status,
        object_type=object_type,
        object_id=object_id,
        period_from=period_from,
        period_to=period_to,
    )
    responsible_ids = {item.responsible_user_id for item in items if item.responsible_user_id}
    names = await auth_service.user_names(db, responsible_ids)

    def _responsible(item: RiskIndicator) -> str:
        return names.get(item.responsible_user_id, "") if item.responsible_user_id else ""

    rows = [RiskIndicatorRow(item, responsible=_responsible(item)) for item in items]
    return rows, total, cap


def render_risk_indicators(items: list[RiskIndicatorRow], *, lang: xlsx.Lang) -> bytes:
    title = TITLE_RISK_INDICATORS[lang]
    return xlsx.render(items, columns_risk_indicators(lang), lang=lang, title=title)


def columns_events(lang: xlsx.Lang) -> list[xlsx.Column[OversightEvent]]:
    return [
        # Kept RAW (never translated), same posture as `code` above: an
        # event name is an identifier, not a vocabulary word — unlike
        # `object_type`/`rn_status`, the adminka's own dictionary has no
        # `leadership.oversight.event.*` entry for every possible value.
        xlsx.Column(
            "event_type", {"uz_latn": "Hodisa", "ru": "Событие"}, lambda r: r.event_type, 26
        ),
        xlsx.Column(
            "object_type",
            {"uz_latn": "Obyekt turi", "ru": "Тип объекта"},
            lambda r: _label(OBJECT_TYPE_LABELS, r.object_type, lang),
            18,
        ),
        xlsx.Column(
            "object_id",
            {"uz_latn": "Obyekt", "ru": "Объект"},
            lambda r: str(r.object_id) if r.object_id else "",
            38,
        ),
        xlsx.Column(
            "correlation_id",
            {"uz_latn": "Korrelyatsiya", "ru": "Корреляция"},
            lambda r: r.correlation_id or "",
            22,
        ),
        xlsx.Column("occurred_at", {"uz_latn": "Vaqt", "ru": "Время"}, lambda r: r.occurred_at, 18),
        xlsx.Column(
            "rn_status",
            {"uz_latn": "RN holati", "ru": "Статус RN"},
            lambda r: _label(RN_STATUS_LABELS, r.rn_status, lang),
            16,
        ),
        xlsx.id_column(),
    ]


async def rows_events(
    db: AsyncSession,
    *,
    actor: User,
    lang: xlsx.Lang,
    event_type: str | None,
    object_type: str | None,
    period_from: date | None,
    period_to: date | None,
) -> tuple[list[OversightEvent], int, int]:
    """No zone predicate (`service.list_events`'s own docstring) — gated on
    `oversight.view` alone, same as the risk-indicator export above."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    items, total = await service.list_events(
        db,
        actor=actor,
        params=PageParams.model_construct(page=1, page_size=cap),
        event_type=event_type,
        object_type=object_type,
        period_from=period_from,
        period_to=period_to,
    )
    return list(items), total, cap


def render_events(items: list[OversightEvent], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, columns_events(lang), lang=lang, title=TITLE_EVENTS[lang])
