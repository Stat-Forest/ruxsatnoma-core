"""`GET /gis/contours/export.xlsx` and `GET /gis/imports/export.xlsx`
(stage 13, ruling #204): the two registers on paper.

Both `rows_*` functions call the SAME service function their list route
calls, with the same filters and the same `actor` (so zone scoping cannot
diverge from the screen — ruling R2). Every id the sheet shows is resolved
to a name in ONE batch query per table; column headers and status labels are
copied from the adminka's `gis/contours/ContoursTab.tsx` and
`gis/imports/ImportsTab.tsx` (+ `i18n/*.ts`) so the file reads like the
screen. NO geometry ever enters either sheet."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.admin import service as admin_service
from app.modules.gis import service
from app.modules.gis.models import Contour

# --- contours ------------------------------------------------------------

CONTOURS_TITLE = {"uz_latn": "Konturlar", "ru": "Контуры"}

KIND_LABELS: dict[str, dict[str, str]] = {
    "contour": {"uz_latn": "Kontur", "ru": "Контур"},
    "subcontour": {"uz_latn": "Subkontur", "ru": "Подконтур"},
}
BOOL_LABELS: dict[bool, dict[str, str]] = {
    True: {"uz_latn": "Ha", "ru": "Да"},
    False: {"uz_latn": "Yoʻq", "ru": "Нет"},
}


def _label(table: dict[str, dict[str, str]], code: str, lang: xlsx.Lang) -> str:
    """An unknown code renders as itself — never an empty cell that hides
    it (same rule Track A's `applications/export.py` states for its own
    status column)."""
    return table.get(code, {}).get(lang, code)


def _bool_label(value: bool, lang: xlsx.Lang) -> str:
    return BOOL_LABELS[value][lang]


class ContourRow:
    """One `GET /gis/contours` item (`ContourListItem`'s own dict shape,
    ruling R4's screen columns) plus the identity columns the LIST route
    does not carry at all (`kind`, `layer_id`, `parent_id`, `created_at` —
    resolved by a second batch query against `Contour` itself, scoped to
    the SAME ids the list already returned, never widening what the caller
    may see) and the names those ids resolve to."""

    def __init__(
        self,
        item: dict[str, Any],
        *,
        organization: str,
        layer: str,
        kind: str,
        parent_number: str,
        created_at: Any,
    ) -> None:
        self.id = item["id"]
        self.number = item["number"]
        self.area_ha = item["area_ha"]
        self.occupied_ha = item["occupied_ha"]
        self.s_available_ha = item["s_available_ha"]
        self.over_allocated = item["over_allocated"]
        self.organization = organization
        self.layer = layer
        self.kind = kind
        self.parent_number = parent_number
        self.created_at = created_at


def contours_columns(lang: xlsx.Lang) -> list[xlsx.Column[ContourRow]]:
    return [
        xlsx.Column("number", {"uz_latn": "Raqam", "ru": "Номер"}, lambda r: r.number, 16),
        xlsx.Column(
            "organization",
            {"uz_latn": "Tashkilot", "ru": "Организация"},
            lambda r: r.organization,
            30,
        ),
        xlsx.Column("layer", {"uz_latn": "Qatlam", "ru": "Слой"}, lambda r: r.layer, 20),
        xlsx.Column(
            "kind",
            {"uz_latn": "Turi", "ru": "Тип"},
            lambda r: _label(KIND_LABELS, r.kind, lang),
            14,
        ),
        xlsx.Column(
            "parent_number",
            {"uz_latn": "Yuqori kontur", "ru": "Родительский контур"},
            lambda r: r.parent_number,
            16,
        ),
        xlsx.Column(
            "area_ha",
            {"uz_latn": "Umumiy maydon, ga", "ru": "Общая площадь, га"},
            lambda r: r.area_ha,
            16,
        ),
        xlsx.Column(
            "occupied_ha",
            {"uz_latn": "Band qism, ga", "ru": "Занятая часть, га"},
            lambda r: r.occupied_ha,
            16,
        ),
        xlsx.Column(
            "s_available_ha",
            {"uz_latn": "Mavjud maydon, ga", "ru": "Доступная площадь, га"},
            lambda r: r.s_available_ha,
            18,
        ),
        xlsx.Column(
            "over_allocated",
            {"uz_latn": "Meʼyordan ortiq band", "ru": "Занято сверх нормы"},
            lambda r: _bool_label(r.over_allocated, lang),
            16,
        ),
        xlsx.Column(
            "created_at", {"uz_latn": "Yaratilgan", "ru": "Создан"}, lambda r: r.created_at, 18
        ),
        xlsx.id_column(),
    ]


async def rows_contours(
    db: AsyncSession,
    *,
    actor: Any,
    lang: xlsx.Lang,
    organization_id: uuid.UUID | None,
    bbox: str | None,
) -> tuple[list[ContourRow], int, int]:
    """(rows, total, cap). `service.list_contours` is exactly what
    `GET /gis/contours` calls — same zone scoping (`zone_filter`), same
    published-version-only join, same bbox validation. The identity columns
    the list route does not carry (`kind`/`layer_id`/`parent_id`/
    `created_at`) are read back from `Contour` for the SAME ids the list
    already returned — this never widens what the caller may see, only what
    is printed about a row it already showed them."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    items, total = await service.list_contours(
        db,
        organization_id=organization_id,
        bbox=bbox,
        params=PageParams.model_construct(page=1, page_size=cap),
        actor=actor,
    )
    ids = {item["id"] for item in items}
    identity_columns = (
        Contour.id,
        Contour.kind,
        Contour.layer_id,
        Contour.parent_id,
        Contour.created_at,
    )
    identity_rows = (
        (await db.execute(select(*identity_columns).where(Contour.id.in_(ids)))).all()
        if ids
        else []
    )
    identity_by_id = {row.id: row for row in identity_rows}
    orgs = await admin_service.organization_names(db, {item["organization_id"] for item in items})
    layers = {layer.id: dict(layer.name) for layer in await service.list_layers(db)}
    parent_ids = {row.parent_id for row in identity_rows if row.parent_id}
    parent_numbers = await service.contour_numbers_by_ids(db, parent_ids)
    rows = []
    for item in items:
        identity = identity_by_id.get(item["id"])
        rows.append(
            ContourRow(
                item,
                organization=xlsx.localized(orgs.get(item["organization_id"]), lang),
                layer=xlsx.localized(layers.get(identity.layer_id), lang) if identity else "",
                kind=identity.kind if identity else "",
                parent_number=parent_numbers.get(identity.parent_id, "")
                if identity and identity.parent_id
                else "",
                created_at=identity.created_at if identity else None,
            )
        )
    return rows, total, cap


def render_contours(items: Sequence[ContourRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, contours_columns(lang), lang=lang, title=CONTOURS_TITLE[lang])
