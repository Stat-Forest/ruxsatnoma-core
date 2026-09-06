"""Give every GIS layer an Uzbek Latin name.

`gis_layers.name` was seeded by `0010_gis.py` from tuples shaped
`(code, uz_cyrl, ru, geometry_type, is_public)` — so the JSONB holds exactly two
keys, `uz_cyrl` and `ru`, and **no `uz_latn`**, for all fifteen layers.

Uzbek Latin is the project's base language (decision #18): every other language
falls back to it, and the public site renders in it. So the open-data page at
`/opendata` listed its four public layers in Cyrillic — «Ёнғин тақиқлари»,
«Ўрмон фонди чегаралари» — in the middle of Latin text, because that is the only
Uzbek the catalogue had. The adminka's GIS screens read the same rows.

No test could have caught it. The payload is well-formed and nothing is empty:
the key the Latin page asks for was simply never put in the data. The front end's
own i18n parity tests check the application's translation files, which are
complete and symmetric — this is data, not translations.

The column is JSONB, so this is an `UPDATE` per row, not a schema change. Rows
are matched by `code`, and `||` merges the new key in rather than replacing the
object, so a name an operator has since edited keeps its other languages.

`rotation` is included, but it stays a placeholder in a different script rather
than becoming a translation: `0010` seeded it with the identical string "Ротация"
in both `uz_cyrl` and `ru`, so "Rotatsiya" here is the same non-translation in
Latin. Leaving it in Cyrillic would have left this row showing the exact bug the
rest of the migration fixes, which preserves nothing useful — the placeholder is
still visible in that all three languages say the same word. The Agency's real
term for the pasture-rotation layer is still outstanding.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# code -> uz_latn, transliterated from the `uz_cyrl` string `0010_gis.py` seeded.
UZ_LATN_NAMES: dict[str, str] = {
    "forest_fund": "Oʻrmon fondi chegaralari",
    "org_boundaries": "Tashkilot chegaralari",
    "contours": "Konturlar",
    "pastures": "Yaylovlar",
    "hayfields": "Pichanzorlar",
    "apiaries": "Asalarichilik joylari",
    "recreation": "Rekreatsiya hududlari",
    "restrictions": "Cheklovlar",
    "protection": "Muhofaza zonalari",
    "rotation": "Rotatsiya",
    "rest_calendar": "Dam berish taqvimi",
    "water_points": "Suv nuqtalari",
    "cattle_corridors": "Chorva yoʻlaklari",
    "fire_bans": "Yongʻin taqiqlari",
    "special_areas": "Maxsus ajratilgan maydonlar",
}


def upgrade() -> None:
    for code, uz_latn in UZ_LATN_NAMES.items():
        op.execute(
            sa.text(
                "UPDATE gis_layers "
                "SET name = name || jsonb_build_object('uz_latn', CAST(:uz_latn AS text)) "
                "WHERE code = :code"
            ).bindparams(code=code, uz_latn=uz_latn)
        )


def downgrade() -> None:
    for code in UZ_LATN_NAMES:
        op.execute(
            sa.text("UPDATE gis_layers SET name = name - 'uz_latn' WHERE code = :code").bindparams(
                code=code
            )
        )
