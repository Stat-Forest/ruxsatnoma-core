"""permits

Schema for stage 3.11a (`design/02` § permits, corrected by plan
`03.11a-permits-core`): all seven tables the module hangs off, the series counter,
the four permission codes, the default grazing template and this module's
notification templates.

All seven land now, including `permit_duplicates`, `forest_tickets` and
`qr_check_log`, whose writers are 3.11b and Task 5 — a table costs nothing to create
and a migration in a block shared with two other sessions costs a coordination round.
`permits.status` carries all six `tz/05` statuses from day one even though 3.11a
writes only three (`suspended`/`revoked` are 3.11b's, `archived` is 4.7's), so none of
them needs a migration to widen a constraint. `permits.doc_hash` is a column
`design/02` does not have — ruling 3: the PDF is rendered once and every signature is
taken over exactly those bytes.

Two things autogenerate cannot see and this file therefore writes by hand: the
append-only trigger on `permit_status_history` (mirroring `audit_log`'s in 0002,
`calculations`'s in 0011 and `application_status_history`'s in 0015), and the seeded
rows below.

`down_revision` is `0016`, not `0018`: migrations 0017-0018 belong to the parallel
3.10a payments branch, which has not merged yet (`plans/03.9-3.11-parallel-run.md`).
The two chains are reconciled at merge time by whoever merges second — with
`alembic merge heads`, never by renumbering (lesson).

Revision ID: 0019
Revises: 0016
Create Date: 2026-09-02 13:10:45.700132

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The series `tz/05` and `design/03` name for the permit («А № 000123»). CYRILLIC
# CAPITAL A (U+0410), not Latin A — the two look identical and index differently.
PERMIT_SERIES = "А"

# Fixed id from 0005's ACTIVITY_TYPES seed. A literal rather than a lookup by code on
# purpose: a wrong code in `SELECT ... WHERE code = :x` inserts zero rows silently
# (lesson), while a wrong uuid fails the FK loudly.
GRAZING_ACTIVITY_TYPE_ID = "0198f100-0001-7000-8000-000000000001"

# Role codes read from `0003_auth.py`, never from plan prose (lesson). `permits.manage`
# — 3.11b's suspend/revoke/duplicate — goes to `executor_head` and NOT to `leadership`:
# tz/03's matrix gives «Т» to «Раҳбар» = executor_head, which is exactly what migration
# 0016 corrected project-wide (decision #59). See permits/permissions.py for the full
# citation of each grant.
ROLE_GRANTS: list[tuple[str, str]] = [
    ("executor_staff", "permits.issue"),
    ("executor_head", "permits.sign"),
    ("chief_forester", "permits.sign"),
    ("accountant", "permits.sign"),
    ("applicant", "permits.sign"),
    ("executor_staff", "permits.view_any"),
    ("prosecutor", "permits.view_any"),
    ("executor_head", "permits.manage"),
]
PERMISSION_CODES: tuple[str, ...] = (
    "permits.issue",
    "permits.sign",
    "permits.view_any",
    "permits.manage",
)

# Ruling 17: with no template, `notify()` writes a raw fallback string in-app and sends
# NOTHING at all by SMS or e-mail, silently — while a test asserting "a notification row
# exists" still passes. So every code in `permits.events.NOTIFIED_EVENT_CODES` needs a
# row, and the codes are DOTTED (`notification_templates.event_code`), never the flat
# bus names the plan's prose used.
#
# `permit.issued` is deliberately ABSENT: `0009_notifications.py` already seeded it for
# both channels, and `uq_notification_templates_active` is a partial unique index on
# (event_code, channel) WHERE status='active' — re-seeding it here would fail the
# migration, not merely duplicate a row. A later text change to it is a supersede
# (archive + version 2), which is admin CRUD, not a migration.
#
# SMS bodies stay short: a Cyrillic SMS part bills at 70 characters (0009 ruling 20).
_BODIES: dict[str, dict[str, str]] = {
    "permit.signed": {
        "uz_cyrl": "{permit_number} рухсатномасига имзо қўйилди.",
        "ru": "Разрешение {permit_number} подписано.",
    },
    "permit.active": {
        "uz_cyrl": "Рухсатнома {permit_number} кучга кирди. Муддати: {valid_from} — {valid_to}.",
        "ru": "Разрешение {permit_number} вступило в силу. Срок: {valid_from} — {valid_to}.",
    },
    "permit.expiring": {
        "uz_cyrl": "Рухсатнома {permit_number} муддати {valid_to} да тугайди.",
        "ru": "Срок разрешения {permit_number} истекает {valid_to}.",
    },
    "permit.expired": {
        "uz_cyrl": "Рухсатнома {permit_number} муддати тугади.",
        "ru": "Срок разрешения {permit_number} истёк.",
    },
}
SEED_TEMPLATES: list[tuple[str, str, dict[str, str]]] = [
    (event_code, channel, body)
    for event_code, body in _BODIES.items()
    for channel in ("inapp", "sms")
]


def upgrade() -> None:
    """Upgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "permit_counters",
        sa.Column("series", sa.Text(), nullable=False),
        sa.Column("last_number", sa.BigInteger(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("series", name=op.f("pk_permit_counters")),
    )
    op.create_table(
        "permit_templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("activity_type_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("layout_file_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name=op.f("ck_permit_templates_status_valid"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_permit_templates_version_positive")),
        sa.ForeignKeyConstraint(
            ["activity_type_id"],
            ["activity_types.id"],
            name=op.f("fk_permit_templates_activity_type_id_activity_types"),
        ),
        sa.ForeignKeyConstraint(
            ["layout_file_id"],
            ["media_files.id"],
            name=op.f("fk_permit_templates_layout_file_id_media_files"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permit_templates")),
        sa.UniqueConstraint(
            "activity_type_id", "version", name=op.f("uq_permit_templates_activity_type_id_version")
        ),
    )
    op.create_index(
        op.f("ix_permit_templates_layout_file_id"),
        "permit_templates",
        ["layout_file_id"],
        unique=False,
    )
    op.create_index(
        "uq_permit_templates_active",
        "permit_templates",
        ["activity_type_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "permits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("series", sa.Text(), nullable=False),
        sa.Column("number", sa.BigInteger(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("activity_type_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("contour_id", sa.Uuid(), nullable=False),
        sa.Column("contour_version_id", sa.Uuid(), nullable=False),
        sa.Column("area_ha", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("period_from", sa.Date(), nullable=False),
        sa.Column("period_to", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("sb_load", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("pdf_file_id", sa.Uuid(), nullable=True),
        sa.Column("doc_hash", sa.Text(), nullable=True),
        sa.Column("qr_token", sa.Text(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending_signatures', 'active', 'suspended', 'revoked', 'expired', "
            "'archived')",
            name=op.f("ck_permits_status_valid"),
        ),
        sa.CheckConstraint("number > 0", name=op.f("ck_permits_number_positive")),
        sa.CheckConstraint("period_to >= period_from", name=op.f("ck_permits_period_ordered")),
        sa.ForeignKeyConstraint(
            ["activity_type_id"],
            ["activity_types.id"],
            name=op.f("fk_permits_activity_type_id_activity_types"),
        ),
        sa.ForeignKeyConstraint(
            ["applicant_id"], ["applicants.id"], name=op.f("fk_permits_applicant_id_applicants")
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_permits_application_id_applications"),
        ),
        sa.ForeignKeyConstraint(
            ["contour_id"], ["contours.id"], name=op.f("fk_permits_contour_id_contours")
        ),
        sa.ForeignKeyConstraint(
            ["contour_version_id"],
            ["contour_versions.id"],
            name=op.f("fk_permits_contour_version_id_contour_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_permits_organization_id_organizations"),
        ),
        sa.ForeignKeyConstraint(
            ["pdf_file_id"], ["media_files.id"], name=op.f("fk_permits_pdf_file_id_media_files")
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["permit_templates.id"],
            name=op.f("fk_permits_template_id_permit_templates"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permits")),
        sa.UniqueConstraint("application_id", name=op.f("uq_permits_application_id")),
        sa.UniqueConstraint("qr_token", name=op.f("uq_permits_qr_token")),
        sa.UniqueConstraint("series", "number", name=op.f("uq_permits_series_number")),
    )
    op.create_index(
        op.f("ix_permits_activity_type_id"), "permits", ["activity_type_id"], unique=False
    )
    op.create_index(op.f("ix_permits_applicant_id"), "permits", ["applicant_id"], unique=False)
    op.create_index(op.f("ix_permits_contour_id"), "permits", ["contour_id"], unique=False)
    op.create_index(
        op.f("ix_permits_contour_version_id"), "permits", ["contour_version_id"], unique=False
    )
    op.create_index(
        op.f("ix_permits_organization_id"), "permits", ["organization_id"], unique=False
    )
    op.create_index(op.f("ix_permits_pdf_file_id"), "permits", ["pdf_file_id"], unique=False)
    op.create_index(op.f("ix_permits_template_id"), "permits", ["template_id"], unique=False)
    op.create_table(
        "forest_tickets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("permit_id", sa.Uuid(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=False),
        sa.Column("restrictions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("issued_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'revoked')",
            name=op.f("ck_forest_tickets_status_valid"),
        ),
        sa.CheckConstraint("valid_to >= valid_from", name=op.f("ck_forest_tickets_period_ordered")),
        sa.ForeignKeyConstraint(
            ["file_id"], ["media_files.id"], name=op.f("fk_forest_tickets_file_id_media_files")
        ),
        sa.ForeignKeyConstraint(
            ["issued_by"], ["users.id"], name=op.f("fk_forest_tickets_issued_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["permit_id"], ["permits.id"], name=op.f("fk_forest_tickets_permit_id_permits")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forest_tickets")),
        sa.UniqueConstraint("number", name=op.f("uq_forest_tickets_number")),
    )
    op.create_index(op.f("ix_forest_tickets_file_id"), "forest_tickets", ["file_id"], unique=False)
    op.create_index(
        op.f("ix_forest_tickets_issued_by"), "forest_tickets", ["issued_by"], unique=False
    )
    op.create_index(
        op.f("ix_forest_tickets_permit_id"), "forest_tickets", ["permit_id"], unique=False
    )
    op.create_table(
        "permit_duplicates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("permit_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("issued_by", sa.Uuid(), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["file_id"], ["media_files.id"], name=op.f("fk_permit_duplicates_file_id_media_files")
        ),
        sa.ForeignKeyConstraint(
            ["issued_by"], ["users.id"], name=op.f("fk_permit_duplicates_issued_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["permit_id"], ["permits.id"], name=op.f("fk_permit_duplicates_permit_id_permits")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permit_duplicates")),
    )
    op.create_index(
        op.f("ix_permit_duplicates_file_id"), "permit_duplicates", ["file_id"], unique=False
    )
    op.create_index(
        op.f("ix_permit_duplicates_issued_by"), "permit_duplicates", ["issued_by"], unique=False
    )
    op.create_index(
        op.f("ix_permit_duplicates_permit_id"), "permit_duplicates", ["permit_id"], unique=False
    )
    op.create_table(
        "permit_status_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("permit_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column("reason_item_id", sa.Uuid(), nullable=True),
        sa.Column("legal_basis", sa.Text(), nullable=True),
        sa.Column("doc_file_id", sa.Uuid(), nullable=True),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status IN ('pending_signatures', 'active', 'suspended', "
            "'revoked', 'expired', 'archived')",
            name=op.f("ck_permit_status_history_from_status_valid"),
        ),
        sa.CheckConstraint(
            "to_status IN ('pending_signatures', 'active', 'suspended', 'revoked', 'expired', "
            "'archived')",
            name=op.f("ck_permit_status_history_to_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["changed_by"], ["users.id"], name=op.f("fk_permit_status_history_changed_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["doc_file_id"],
            ["media_files.id"],
            name=op.f("fk_permit_status_history_doc_file_id_media_files"),
        ),
        sa.ForeignKeyConstraint(
            ["permit_id"], ["permits.id"], name=op.f("fk_permit_status_history_permit_id_permits")
        ),
        sa.ForeignKeyConstraint(
            ["reason_item_id"],
            ["classifier_items.id"],
            name=op.f("fk_permit_status_history_reason_item_id_classifier_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permit_status_history")),
    )
    op.create_index(
        op.f("ix_permit_status_history_changed_by"),
        "permit_status_history",
        ["changed_by"],
        unique=False,
    )
    op.create_index(
        op.f("ix_permit_status_history_doc_file_id"),
        "permit_status_history",
        ["doc_file_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_permit_status_history_reason_item_id"),
        "permit_status_history",
        ["reason_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_permit_status_history_timeline",
        "permit_status_history",
        # `id` is part of the KEY, not a payload column: `occurred_at` is `now()`,
        # which is TRANSACTION start time, so two rows written in one transaction
        # share it — see PermitStatusHistory's docstring.
        ["permit_id", "occurred_at", "id"],
        unique=False,
    )
    op.create_table(
        "qr_check_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("permit_id", sa.Uuid(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "channel IN ('qr', 'manual')", name=op.f("ck_qr_check_log_channel_valid")
        ),
        sa.CheckConstraint(
            "result IN ('found', 'not_found')", name=op.f("ck_qr_check_log_result_valid")
        ),
        sa.ForeignKeyConstraint(
            ["permit_id"], ["permits.id"], name=op.f("fk_qr_check_log_permit_id_permits")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qr_check_log")),
    )
    op.create_index(
        "ix_qr_check_log_occurred_at_brin",
        "qr_check_log",
        ["occurred_at"],
        unique=False,
        postgresql_using="brin",
    )
    op.create_index(op.f("ix_qr_check_log_permit_id"), "qr_check_log", ["permit_id"], unique=False)
    # ### end Alembic commands ###

    # permit_status_history is append-only, mirroring audit_log's (0002),
    # calculations's (0011) and application_status_history's (0015) triggers exactly.
    # A plain RAISE EXCEPTION carries SQLSTATE P0001, so it surfaces as DBAPIError and
    # NOT as IntegrityError — the tests assert the former (lesson).
    op.execute(
        """
        CREATE FUNCTION permit_status_history_forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'permit_status_history is append-only: a correction is a new row';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER permit_status_history_append_only "
        "BEFORE UPDATE OR DELETE ON permit_status_history "
        "FOR EACH ROW EXECUTE FUNCTION permit_status_history_forbid_mutation()"
    )
    op.execute(
        "CREATE TRIGGER permit_status_history_no_truncate "
        "BEFORE TRUNCATE ON permit_status_history "
        "FOR EACH STATEMENT EXECUTE FUNCTION permit_status_history_forbid_mutation()"
    )

    # The single counter row. A second series is a second row, not a migration
    # (ruling 9); `last_number` starts at 0, so the first permit issued is «А № 1».
    op.execute(
        sa.text("INSERT INTO permit_counters (series, last_number) VALUES (:s, 0)").bindparams(
            s=PERMIT_SERIES
        )
    )

    # The default template for grazing — and, under `uq_permit_templates_active`, the
    # ONE active row for that activity type until an administrator supersedes it
    # (archive, then insert version 2). `layout_file_id` stays NULL: a migration cannot
    # put bytes in MinIO, and a row pointing at a storage key that does not exist would
    # be worse than an honest null — null means "the layout bundled with the module"
    # (app/modules/permits/assets/, Task 2). An administrator uploading a layout fills
    # the column and that row wins from then on.
    op.execute(
        sa.text(
            "INSERT INTO permit_templates "
            "(id, activity_type_id, version, name, layout_file_id, status, valid_from) "
            "VALUES (CAST(:id AS uuid), CAST(:activity AS uuid), 1, "
            "jsonb_build_object('uz_cyrl', :cyr, 'ru', :ru), NULL, 'active', DATE '2026-01-01')"
        ).bindparams(
            id=str(uuid.uuid4()),
            activity=GRAZING_ACTIVITY_TYPE_ID,
            cyr="Рухсатнома (1-илова) — чорва молларини боқиш",
            ru="Разрешение (1-илова) — выпас скота",
        )
    )

    templates = sa.table(
        "notification_templates",
        sa.column("id", sa.Uuid()),
        sa.column("event_code", sa.Text()),
        sa.column("channel", sa.Text()),
        sa.column("body", postgresql.JSONB(astext_type=sa.Text())),
        sa.column("version", sa.Integer()),
        sa.column("status", sa.Text()),
    )
    op.bulk_insert(
        templates,
        [
            {
                # uuid4(), not app.db.uuid7 — a migration must not depend on app code
                # that could move or be renamed later (0009 made the same call).
                "id": uuid.uuid4(),
                "event_code": event_code,
                "channel": channel,
                "body": body,
                "version": 1,
                "status": "active",
            }
            for event_code, channel, body in SEED_TEMPLATES
        ],
    )

    for role, code in ROLE_GRANTS:
        op.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_code) "
                "SELECT id, :code FROM roles WHERE code = :role "
                "ON CONFLICT DO NOTHING"
            ).bindparams(code=code, role=role)
        )


def downgrade() -> None:
    """Downgrade schema."""
    # Seeds and hand-written invariants, undone in reverse order — a downgrade must
    # delete whatever its upgrade made possible (lesson). The `permit_counters` and
    # `permit_templates` rows need no DELETE of their own: their tables are dropped
    # below. `notifications` rows are deleted BEFORE the templates they point at,
    # or `fk_notifications_template_id_notification_templates` breaks the round-trip.
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_code = ANY(:codes)").bindparams(
            codes=list(PERMISSION_CODES)
        )
    )
    op.execute(
        sa.text("DELETE FROM notifications WHERE event_code = ANY(:codes)").bindparams(
            codes=sorted(_BODIES)
        )
    )
    op.execute(
        sa.text("DELETE FROM notification_templates WHERE event_code = ANY(:codes)").bindparams(
            codes=sorted(_BODIES)
        )
    )
    op.execute("DROP TRIGGER IF EXISTS permit_status_history_no_truncate ON permit_status_history")
    op.execute("DROP TRIGGER IF EXISTS permit_status_history_append_only ON permit_status_history")
    op.execute("DROP FUNCTION IF EXISTS permit_status_history_forbid_mutation()")

    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_qr_check_log_permit_id"), table_name="qr_check_log")
    op.drop_index(
        "ix_qr_check_log_occurred_at_brin", table_name="qr_check_log", postgresql_using="brin"
    )
    op.drop_table("qr_check_log")
    op.drop_index("ix_permit_status_history_timeline", table_name="permit_status_history")
    op.drop_index(
        op.f("ix_permit_status_history_reason_item_id"), table_name="permit_status_history"
    )
    op.drop_index(op.f("ix_permit_status_history_doc_file_id"), table_name="permit_status_history")
    op.drop_index(op.f("ix_permit_status_history_changed_by"), table_name="permit_status_history")
    op.drop_table("permit_status_history")
    op.drop_index(op.f("ix_permit_duplicates_permit_id"), table_name="permit_duplicates")
    op.drop_index(op.f("ix_permit_duplicates_issued_by"), table_name="permit_duplicates")
    op.drop_index(op.f("ix_permit_duplicates_file_id"), table_name="permit_duplicates")
    op.drop_table("permit_duplicates")
    op.drop_index(op.f("ix_forest_tickets_permit_id"), table_name="forest_tickets")
    op.drop_index(op.f("ix_forest_tickets_issued_by"), table_name="forest_tickets")
    op.drop_index(op.f("ix_forest_tickets_file_id"), table_name="forest_tickets")
    op.drop_table("forest_tickets")
    op.drop_index(op.f("ix_permits_template_id"), table_name="permits")
    op.drop_index(op.f("ix_permits_pdf_file_id"), table_name="permits")
    op.drop_index(op.f("ix_permits_organization_id"), table_name="permits")
    op.drop_index(op.f("ix_permits_contour_version_id"), table_name="permits")
    op.drop_index(op.f("ix_permits_contour_id"), table_name="permits")
    op.drop_index(op.f("ix_permits_applicant_id"), table_name="permits")
    op.drop_index(op.f("ix_permits_activity_type_id"), table_name="permits")
    op.drop_table("permits")
    op.drop_index(
        "uq_permit_templates_active",
        table_name="permit_templates",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_index(op.f("ix_permit_templates_layout_file_id"), table_name="permit_templates")
    op.drop_table("permit_templates")
    op.drop_table("permit_counters")
    # ### end Alembic commands ###
