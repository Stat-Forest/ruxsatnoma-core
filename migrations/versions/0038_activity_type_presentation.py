"""activity_type_presentation

Ruling #138: the six services' description and processing term move out of the
landing site's translation files and into the catalog, so an administrator can
edit them. `processing_days` is seeded at 15 for every row — the number
`applications.service.SLA_DAYS` enforces (decision #13) — replacing the "до 3
рабочих дней" the landing printed, which the system has never honoured.

Ruling #138a: this column is what the site DISPLAYS. The deadline the system
enforces stays `SLA_DAYS`; nothing outside `admin`/`norms` may read this one.

Where the copy comes from: verbatim, per `code`, from
`landing/src/i18n/ru/services.ts` and `landing/src/i18n/uz_latn/services.ts`
(`services.items.<name>.desc`). Those keys are camelCase and do not name-match
`activity_types.code` 1:1 — `SELECT code FROM activity_types` gives `grazing`,
`haymaking`, `apiary`, `recreation`, `deadwood`, `science`, so the mapping is
by MEANING: `beekeeping` -> `apiary`. `wildPlants` ("Сбор дикорастущих и
лекарственных растений") has no counterpart among the six: `deadwood` is
specifically dry-branch collection, a different activity, so it is not
force-matched to it. That leaves `deadwood` and `science` with no seeded
description — this migration leaves both NULL rather than inventing copy.

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-07 16:52:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# code -> (uz_latn, ru), copied verbatim from the landing's services.ts pair.
DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "grazing": (
        "Oʻrmon fondi yaylov hududlarida belgilangan normaga muvofiq qoramol, "
        "qoʻy va echkilarni boqish uchun elektron ruxsatnoma.",
        "Электронное разрешение на выпас крупного и мелкого рогатого скота на "
        "пастбищных угодьях лесного фонда в соответствии с установленной нормой.",
    ),
    "haymaking": (
        "Mavsumiy pichan oʻrish maydonlaridan foydalanish va chorva uchun "
        "oziq-ovqat zaxirasini gʻamlash.",
        "Пользование сенокосными угодьями в сезонный период и заготовка "
        "кормовых запасов для скота.",
    ),
    "apiary": (
        "Asalari oilalarini oʻrmon oʻsimliklari gullash davrida oʻrmon "
        "yerlariga vaqtinchalik joylashtirish.",
        "Временное размещение пчелиных семей на землях лесного фонда в "
        "период цветения лесных растений.",
    ),
    "recreation": (
        "Vaqtinchalik yengil inshootlar qurish va ekologik turizm "
        "yoʻnalishida xizmatlar koʻrsatish.",
        "Возведение временных лёгких сооружений и оказание услуг в сфере экологического туризма.",
    ),
}


def upgrade() -> None:
    op.add_column(
        "activity_types",
        sa.Column("description", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "activity_types",
        sa.Column("processing_days", sa.Integer(), nullable=False, server_default=sa.text("15")),
    )
    op.create_check_constraint("processing_days_positive", "activity_types", "processing_days > 0")

    for code, (uz_latn, ru) in DESCRIPTIONS.items():
        op.execute(
            sa.text(
                "UPDATE activity_types "
                "SET description = jsonb_build_object('uz_latn', CAST(:uz_latn AS text), "
                "'ru', CAST(:ru AS text)) "
                "WHERE code = :code"
            ).bindparams(code=code, uz_latn=uz_latn, ru=ru)
        )
    # `deadwood` and `science` have no counterpart in the landing copy (see the
    # module docstring above) — their `description` stays NULL, the column's
    # default on ADD COLUMN.


def downgrade() -> None:
    # Short name here, not the fully-qualified "ck_activity_types_..." — drop_constraint
    # re-runs it through the naming convention just like create_check_constraint does
    # (same lesson as 0007_admin_users.py's pinfl_format).
    op.drop_constraint("processing_days_positive", "activity_types", type_="check")
    op.drop_column("activity_types", "processing_days")
    op.drop_column("activity_types", "description")
