"""benefit verification

Stage 9, ruling #179 (docs/decisions.md; wave-2 track T9): a beekeeper (or any
other benefit claimant) presents a certificate nobody on our side can
validate, so a dedicated central office checks it — and only applications
carrying one.

Five columns on `applications` (`benefit_certificate_no`,
`benefit_verification_status` IN ('not_required', 'pending', 'verified',
'rejected'), `benefit_verified_by`, `benefit_verified_at`,
`benefit_rejection_reason`) — a state machine SEPARATE from `applications.
status`, so a claim rejection is never confused with the head's own
`status='REJECTED'`. `benefit_verification_status` defaults `not_required`
for every existing and future row; `applications.service.submit` is what
moves it to `pending` (out of this track's file ownership — see this
migration's sibling code, `app/modules/applications/benefit_verification.py`,
for the integrator's own note).

One new role, `benefit_verifier` (`is_system=true`, CENTRAL — no
`organization_id`/`region_id`/`district_id` is assigned to its holders,
mirroring `prosecutor`), and one new permission, `benefits.verify`, granted
to it. Seeded the way `0015_applications.py` seeds `ROLE_GRANTS` for an
EXISTING role; this is additionally the first migration since `0003_auth.py`
to insert a new row into `roles` itself, so `name` carries `uz_latn` (decision
#90 — required since `0032`'s backfill, unlike `0003`'s own eleven rows).

A partial index, `ix_applications_benefit_verification_pending`, serves the
office's own country-wide query (`repo.list_certificate_claims`) — WHERE
`benefit_verification_status <> 'not_required'`, which is every row this
office may ever read and a small fraction of the table otherwise.

Revision ID: 0049
Revises: 0048
Create Date: 2026-09-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0051"
down_revision: str | Sequence[str] | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Continues `0003_auth.py`'s own sequential scheme (…0001 through …000b, the
# eleven `is_system` roles that migration seeds) rather than a random uuid4 —
# this is the first row `roles` has gained since.
BENEFIT_VERIFIER_ROLE_ID = "0198f000-0000-7000-8000-00000000000c"
BENEFIT_VERIFIER_CODE = "benefit_verifier"
BENEFITS_VERIFY_CODE = "benefits.verify"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("applications", sa.Column("benefit_certificate_no", sa.Text(), nullable=True))
    op.add_column(
        "applications",
        sa.Column(
            "benefit_verification_status",
            sa.Text(),
            server_default="not_required",
            nullable=False,
        ),
    )
    op.add_column("applications", sa.Column("benefit_verified_by", sa.Uuid(), nullable=True))
    op.add_column(
        "applications",
        sa.Column("benefit_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("applications", sa.Column("benefit_rejection_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "benefit_verification_status_valid",
        "applications",
        "benefit_verification_status IN ('not_required', 'pending', 'verified', 'rejected')",
    )
    op.create_foreign_key(
        op.f("fk_applications_benefit_verified_by_users"),
        "applications",
        "users",
        ["benefit_verified_by"],
        ["id"],
    )
    op.create_index(
        op.f("ix_applications_benefit_verified_by"),
        "applications",
        ["benefit_verified_by"],
        unique=False,
    )
    # `repo.list_certificate_claims`'s own query, materialised: every row the
    # office may ever read, and (`benefit_categories` still ships empty,
    # ruling #179's own closing paragraph) zero rows until VMQ 278's list and
    # the first real claim both exist.
    op.create_index(
        "ix_applications_benefit_verification_pending",
        "applications",
        ["benefit_verification_status"],
        unique=False,
        postgresql_where=sa.text("benefit_verification_status <> 'not_required'"),
    )

    op.execute(
        sa.text(
            "INSERT INTO roles (id, code, name, is_system, status) "
            "VALUES (CAST(:id AS uuid), :code, "
            "jsonb_build_object('uz_latn', :latn, 'uz_cyrl', :cyr, 'ru', :ru, 'en', :en), "
            "true, 'active')"
        ).bindparams(
            id=BENEFIT_VERIFIER_ROLE_ID,
            code=BENEFIT_VERIFIER_CODE,
            latn="Imtiyoz sertifikatlarini tekshiruvchi",
            cyr="Имтиёз сертификатларини текширувчи",
            ru="Проверяющий льготных сертификатов",
            en="Benefit certificate verifier",
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_code) "
            "SELECT id, :code FROM roles WHERE code = :role "
            "ON CONFLICT DO NOTHING"
        ).bindparams(code=BENEFITS_VERIFY_CODE, role=BENEFIT_VERIFIER_CODE)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_code = :code").bindparams(
            code=BENEFITS_VERIFY_CODE
        )
    )
    # Lesson: "a downgrade must delete whatever its upgrade made possible" —
    # `users.role_id` is NOT NULL, so a real `benefit_verifier` account (this
    # role's whole point is several real users) blocks the bare `DELETE`
    # below with `fk_users_role_id_roles` the moment one exists; invisible on
    # an untouched database, real the instant this stage's own tests commit
    # one (`tests/modules/applications/test_benefit_verification.py`'s
    # `benefit_verifier_client`). Reassigned to `applicant` — seeded by
    # `0003_auth.py`, so it is guaranteed to still exist at THIS point in the
    # downgrade chain — deliberately the LEAST-privileged role rather than a
    # staff one, so a downgrade never leaves a demoted account MORE able than
    # before (fail closed, the same posture `0046`'s own downgrade states for
    # folding a configurable split back into a fixed bucket).
    op.execute(
        sa.text(
            "UPDATE users SET role_id = (SELECT id FROM roles WHERE code = 'applicant') "
            "WHERE role_id = (SELECT id FROM roles WHERE code = :code)"
        ).bindparams(code=BENEFIT_VERIFIER_CODE)
    )
    op.execute(
        sa.text("DELETE FROM roles WHERE code = :code").bindparams(code=BENEFIT_VERIFIER_CODE)
    )
    op.drop_index("ix_applications_benefit_verification_pending", table_name="applications")
    op.drop_index(op.f("ix_applications_benefit_verified_by"), table_name="applications")
    op.drop_constraint(
        op.f("fk_applications_benefit_verified_by_users"), "applications", type_="foreignkey"
    )
    op.drop_constraint("benefit_verification_status_valid", "applications", type_="check")
    op.drop_column("applications", "benefit_rejection_reason")
    op.drop_column("applications", "benefit_verified_at")
    op.drop_column("applications", "benefit_verified_by")
    op.drop_column("applications", "benefit_verification_status")
    op.drop_column("applications", "benefit_certificate_no")
