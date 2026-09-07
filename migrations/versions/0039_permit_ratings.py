"""permit_ratings

Plan `07.7-services-catalog-and-ratings` task 3 (rulings #140-#142): the citizen's
verdict on a permit they actually received, one row per permit, 1-5, comment
optional. `app/modules/permits/models.py::PermitRating` is the ORM side.

`ratings.view` (ruling #142, zone-scoped exactly like `dashboard.view`) is granted
here to the same shape `0028_oversight.py` already used for a national/leshoz/
oversight read: `central_admin` and `leadership` see everything, `executor_head`
sees their own leshoz through `zone_filter`, `prosecutor` per decision #95.

The plan's own draft named a fourth role `regional_admin` — verified against
`0003_auth.py` and there is no such `roles.code` at all (the eleven seeded roles
are `sys_admin`, `central_admin`, `leadership`, `executor_staff`,
`gis_specialist`, `executor_head`, `chief_forester`, `inspector`, `accountant`,
`applicant`, `prosecutor`). An `INSERT ... SELECT ... WHERE code = 'regional_admin'`
would insert zero rows and fail silently — the exact "rahbar" trap
`.claude/lessons.md` already names. `leadership` (`Агентлик раҳбарияти`, Agency
leadership) is the role that reads across leshozes/regions in this system and is
the one `0028_oversight.py::OVERSIGHT_VIEW_ROLES` grants the equivalent
national-oversight read alongside these same three roles, so it stands in for the
non-existent code here.

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-07 17:46:53.411656

Integration note, corrected 2026-09-08: this revision was authored on parent
`0038`, and `dev` had independently grown its own `0040 -> 0041 -> 0042`
chain off `0037` (see that chain's own note in `0040`), producing two heads,
`0039` and `0042`. An earlier ruling re-pointed `0039`'s `down_revision` from
`0038` to `0042` to collapse the two branches into one line without a merge
revision — that was wrong: Alembic stores only the current head and never
walks back for an ancestor spliced in underneath an already-applied
revision, so a `dev` database already at `0042` would apply `0039` alone on
its next `upgrade head` and never gain `0038`'s two catalog columns. This
migration's `down_revision` is restored to its original `0038`; the two
heads are reconciled properly by `merge_0039_0042_...`'s own
`down_revision = ("0039", "0042")`.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Role codes verified against `0003_auth.py`, never from plan prose (lesson: a
# wrong code inserts zero rows, silently). See the module docstring above for why
# `leadership` stands in for the plan's non-existent `regional_admin`.
RATINGS_VIEW_ROLES: tuple[str, ...] = ("central_admin", "leadership", "executor_head", "prosecutor")


def upgrade() -> None:
    op.create_table(
        "permit_ratings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("permit_id", sa.Uuid(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("score BETWEEN 1 AND 5", name=op.f("ck_permit_ratings_score_valid")),
        sa.ForeignKeyConstraint(
            ["permit_id"], ["permits.id"], name=op.f("fk_permit_ratings_permit_id_permits")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permit_ratings")),
    )
    op.create_index(
        op.f("ix_permit_ratings_permit_id"), "permit_ratings", ["permit_id"], unique=True
    )

    for role in RATINGS_VIEW_ROLES:
        op.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_code) "
                "SELECT id, 'ratings.view' FROM roles WHERE code = :role "
                "ON CONFLICT DO NOTHING"
            ).bindparams(role=role)
        )


def downgrade() -> None:
    op.execute("DELETE FROM role_permissions WHERE permission_code = 'ratings.view'")
    op.drop_index(op.f("ix_permit_ratings_permit_id"), table_name="permit_ratings")
    op.drop_table("permit_ratings")
