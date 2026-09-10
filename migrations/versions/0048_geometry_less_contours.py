"""geometry-less contours

Stage 9 track T5 (decision #178, plan `09-odilxon-demo-fixes.md`). The Agency says
the GIS layers for most leshozes are not ready and gave no date; waiting is not an
option, so a contour may exist without geometry at all — number, forestry unit,
quarter, plot and the area FROM THE DOCUMENTS (`contour_versions.declared_area_ha`,
which already exists beside the computed `area_ha`).

Two independent changes:

1. `contour_versions.geom` becomes nullable, guarded by a new CHECK
   (`geom_or_declared_area`) that a version carries either geometry or a declared
   area — never neither. `area_ha` itself stays NOT NULL and > 0 unchanged
   (`ck_contour_versions_area_positive`): for a geometry-less version the SERVICE
   layer copies `declared_area_ha` onto it on write (`gis.repo.insert_version`/
   `gis.service.update_version`), so every downstream reader keeps reading one
   column and needs no branch for which kind of version it has.
2. `organizations.gis_enabled` — boolean, default true. False means that leshoz
   files contours by requisites and shows no map; editable through the existing
   `PATCH /admin/organizations/{id}` (nothing grants `admin.organizations.manage`
   to any seeded role today, so this is `sys_admin`-only in practice, matching
   "editable by the central admin").

Revision ID: 0047
Revises: 0046
Create Date: 2026-09-10 02:37:28.294579

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry

# revision identifiers, used by Alembic.
revision: str = "0048"
down_revision: str | Sequence[str] | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GEOMETRY_TYPE = Geometry("MULTIPOLYGON", srid=4326, spatial_index=True)


def upgrade() -> None:
    op.alter_column("contour_versions", "geom", existing_type=_GEOMETRY_TYPE, nullable=True)
    op.create_check_constraint(
        op.f("ck_contour_versions_geom_or_declared_area"),
        "contour_versions",
        "geom IS NOT NULL OR declared_area_ha IS NOT NULL",
    )
    op.add_column(
        "organizations",
        sa.Column("gis_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    # A geometry-less version is only possible from this migration onward, so no
    # row predating it can have geom IS NULL — the DELETE below only ever touches
    # data created after this migration ran forward (lesson: "A downgrade must
    # delete whatever its upgrade made possible"). Nothing in this worktree's own
    # test suite attaches such a row to an application or permit, so no FK from
    # those tables is expected to block this; a genuine reference would correctly
    # fail the downgrade loudly rather than silently orphan it.
    op.execute(sa.text("DELETE FROM contour_versions WHERE geom IS NULL"))
    op.drop_column("organizations", "gis_enabled")
    op.drop_constraint(
        op.f("ck_contour_versions_geom_or_declared_area"), "contour_versions", type_="check"
    )
    op.alter_column("contour_versions", "geom", existing_type=_GEOMETRY_TYPE, nullable=False)
