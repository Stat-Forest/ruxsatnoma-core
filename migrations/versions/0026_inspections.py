"""inspections

Plan `04.1-inspections` (stage 4, track B1). The seven tables `design/02-shema-bd.md`
already specifies for this module (`checklists`, `inspection_tasks`,
`inspection_acts`, `inspection_act_files`, `violation_cases`, `violation_appeals`,
`violation_case_history`), plus what design/02's own text does not cover:

1. **`organization_id` on `inspection_tasks`/`inspection_acts`/`violation_cases`**
   (plan's "Schema" ruling) — resolved once at creation, stored directly, the same
   shape `applications.assigned_org_id`/`permits.organization_id` already use rather
   than re-deriving a zone on every read via a join.
2. **`violation_case_history` is append-only** at the database level, mirroring
   `audit_log` (0002), `calculations` (0011), `application_status_history` and
   `permit_status_history` (0015, 0019) exactly — a row-level trigger on
   UPDATE/DELETE, a statement-level one on TRUNCATE.
3. **Six items on the `violation_types` classifier**, VT-01…VT-06 — `0005_admin_
   seeds.py` already created this classifier EMPTY (the same shape `doc_types`/
   `benefit_categories` shipped in), so this migration resolves its existing id
   and inserts items under it, `ON CONFLICT DO NOTHING` against the partial
   unique index, the exact defensive shape `0024_benefit_proof_doc_type.py`
   uses for the identical situation — never a fresh `INSERT INTO classifiers`,
   which would collide with `0005`'s own `uq_classifiers_code`.
4. **One default checklist** (`field_inspection_default`, `activity_type_id=NULL` —
   applies to any activity) so an act has something to reference from day one.
5. **Five permission codes** this module owns, granted to the roles
   `inspections/permissions.py` documents.
6. **One grant that belongs to `permits`' own code, not a new one of ours**: plan
   ruling 3 found that migration `0019` never gave `inspector` the
   `permits.view_any` grant tz/03's role matrix describes ("Инспектор: К" on
   "Разрешение") — `permits._readable_permit` would refuse every inspector without
   it. Recorded here since this is the module that found the gap.

`down_revision = "0023"` while it is the current head — this branch's own worktree
was created alone (fleet contract `docs/plans/04-06-parallel-run.md`); three sibling
tracks are each creating their own revision off `0023` at the same time, and
whichever merges first keeps it, everyone else re-points before their PR.

Revision ID: 0026
Revises: 0023
Create Date: 2026-09-06 01:21:42.162188

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `0005_admin_seeds.py` already created this classifier (id
# `0198f100-0003-7000-8000-000000000004`), empty — its items are ours to add.
CLASSIFIER_CODE = "violation_types"

# (code, id, uz_cyrl name, ru name, sort_order) — tz/04 С16's own examples
# ("превышение нормы, без разрешения, вне контура, нарушение пожарных правил…").
VIOLATION_TYPES: list[tuple[str, str, str, str, int]] = [
    (
        "VT-01",
        "0198f100-0026-7000-8000-000000000002",
        "Норма ёки лимитдан ошиб кетиш",
        "Превышение нормы или лимита",
        10,
    ),
    (
        "VT-02",
        "0198f100-0026-7000-8000-000000000003",
        "Рухсатномасиз фаолият",
        "Деятельность без разрешения",
        20,
    ),
    (
        "VT-03",
        "0198f100-0026-7000-8000-000000000004",
        "Контур ташқарисида фаолият",
        "Деятельность вне контура",
        30,
    ),
    (
        "VT-04",
        "0198f100-0026-7000-8000-000000000005",
        "Ёнғин хавфсизлиги қоидалари бузилиши (ВМҚ 506)",
        "Нарушение правил пожарной безопасности (ПКМ №506)",
        40,
    ),
    (
        "VT-05",
        "0198f100-0026-7000-8000-000000000006",
        "Рухсатнома шартларининг бузилиши",
        "Нарушение условий разрешения",
        50,
    ),
    (
        "VT-06",
        "0198f100-0026-7000-8000-000000000007",
        "Бошқа (изоҳ мажбурий)",
        "Другое (комментарий обязателен)",
        60,
    ),
]

CHECKLIST_ID = "0198f100-0026-7000-8000-000000000008"
CHECKLIST_CODE = "field_inspection_default"
# One question per fact tz/04 С15 names for the checklist ("поголовье по видам,
# площадь, вид деятельности, ульи, сено — сверка с нормой"), plus the two
# structural questions every act answers regardless of activity.
CHECKLIST_ITEMS: list[dict[str, object]] = [
    {
        "code": "activity_matches",
        "question": {
            "uz_cyrl": "Фаолият тури рухсатномага мос келадими?",
            "ru": "Вид деятельности соответствует разрешению?",
        },
        "type": "bool",
        "required": True,
    },
    {
        "code": "within_contour",
        "question": {
            "uz_cyrl": "Фаолият контур чегарасида олиб борилмоқдами?",
            "ru": "Деятельность ведётся в границах контура?",
        },
        "type": "bool",
        "required": True,
    },
    {
        "code": "head_count",
        "question": {
            "uz_cyrl": "Чорва моллари сони (турлар бўйича)",
            "ru": "Поголовье скота (по видам)",
        },
        "type": "number",
        "required": False,
    },
    {
        "code": "area_ha",
        "question": {"uz_cyrl": "Фойдаланилаётган майдон, га", "ru": "Используемая площадь, га"},
        "type": "number",
        "required": False,
    },
    {
        "code": "hives_count",
        "question": {"uz_cyrl": "Асаларичилик уяларининг сони", "ru": "Количество ульев"},
        "type": "number",
        "required": False,
    },
    {
        "code": "hay_volume",
        "question": {
            "uz_cyrl": "Тайёрланган пичан ҳажми (тонна/м3)",
            "ru": "Объём заготовленного сена (тонн/м3)",
        },
        "type": "number",
        "required": False,
    },
    {
        "code": "notes",
        "question": {"uz_cyrl": "Қўшимча изоҳ", "ru": "Дополнительное примечание"},
        "type": "text",
        "required": False,
    },
]

# Role codes read from `0003_auth.py`, never plan prose (lesson: a wrong code
# inserts zero rows silently). See `inspections/permissions.py` for the full
# citation of each grant. The last row is the `permits.view_any` fix (point 6
# of this file's own docstring) — a grant on ANOTHER module's permission code,
# not a new one of ours.
ROLE_GRANTS: list[tuple[str, str]] = [
    ("executor_staff", "inspections.tasks.manage"),
    ("executor_head", "inspections.tasks.manage"),
    ("inspector", "inspections.acts.write"),
    ("executor_head", "inspections.view_any"),
    ("central_admin", "inspections.view_any"),
    ("leadership", "inspections.view_any"),
    ("prosecutor", "inspections.view_any"),
    ("executor_head", "inspections.cases.manage"),
    ("central_admin", "inspections.checklists.manage"),
    ("inspector", "permits.view_any"),
]
PERMISSION_CODES: tuple[str, ...] = (
    "inspections.tasks.manage",
    "inspections.acts.write",
    "inspections.view_any",
    "inspections.cases.manage",
    "inspections.checklists.manage",
)


def upgrade() -> None:
    """Upgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "checklists",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("activity_type_id", sa.Uuid(), nullable=True),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
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
            "status IN ('draft', 'active', 'archived')", name=op.f("ck_checklists_status_valid")
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_checklists_version_positive")),
        sa.ForeignKeyConstraint(
            ["activity_type_id"],
            ["activity_types.id"],
            name=op.f("fk_checklists_activity_type_id_activity_types"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_checklists_created_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checklists")),
        sa.UniqueConstraint("code", "version", name="uq_checklists_code_version"),
    )
    op.create_index(
        op.f("ix_checklists_activity_type_id"), "checklists", ["activity_type_id"], unique=False
    )
    op.create_index(
        "uq_checklists_active_code",
        "checklists",
        ["code"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "inspection_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("permit_id", sa.Uuid(), nullable=True),
        sa.Column("contour_id", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_to", sa.Uuid(), nullable=False),
        sa.Column("due_at", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('pre_approval_visit', 'permit_inspection')",
            name=op.f("ck_inspection_tasks_kind_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('assigned', 'in_progress', 'done', 'cancelled')",
            name=op.f("ck_inspection_tasks_status_valid"),
        ),
        sa.CheckConstraint(
            "application_id IS NOT NULL OR permit_id IS NOT NULL OR contour_id IS NOT NULL",
            name=op.f("ck_inspection_tasks_has_a_subject"),
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_inspection_tasks_application_id_applications"),
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to"], ["users.id"], name=op.f("fk_inspection_tasks_assigned_to_users")
        ),
        sa.ForeignKeyConstraint(
            ["contour_id"], ["contours.id"], name=op.f("fk_inspection_tasks_contour_id_contours")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_inspection_tasks_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_inspection_tasks_organization_id_organizations"),
        ),
        sa.ForeignKeyConstraint(
            ["permit_id"], ["permits.id"], name=op.f("fk_inspection_tasks_permit_id_permits")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inspection_tasks")),
    )
    op.create_index(
        op.f("ix_inspection_tasks_application_id"),
        "inspection_tasks",
        ["application_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_inspection_tasks_assigned_to"), "inspection_tasks", ["assigned_to"], unique=False
    )
    op.create_index(
        op.f("ix_inspection_tasks_contour_id"), "inspection_tasks", ["contour_id"], unique=False
    )
    op.create_index(
        op.f("ix_inspection_tasks_organization_id"),
        "inspection_tasks",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_inspection_tasks_permit_id"), "inspection_tasks", ["permit_id"], unique=False
    )
    op.create_geospatial_table(  # pyright: ignore[reportAttributeAccessIssue]
        "inspection_acts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("permit_id", sa.Uuid(), nullable=True),
        sa.Column("application_id", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("inspector_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "gps",
            Geometry(
                geometry_type="POINT",
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=True,
        ),
        sa.Column("gps_accuracy_m", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("distance_to_contour_m", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("checklist_id", sa.Uuid(), nullable=False),
        sa.Column("answers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_offline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
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
            "result IS NULL OR result IN ('compliant', 'warning', 'violation')",
            name=op.f("ck_inspection_acts_result_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'signed')", name=op.f("ck_inspection_acts_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_inspection_acts_application_id_applications"),
        ),
        sa.ForeignKeyConstraint(
            ["checklist_id"],
            ["checklists.id"],
            name=op.f("fk_inspection_acts_checklist_id_checklists"),
        ),
        sa.ForeignKeyConstraint(
            ["inspector_id"], ["users.id"], name=op.f("fk_inspection_acts_inspector_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_inspection_acts_organization_id_organizations"),
        ),
        sa.ForeignKeyConstraint(
            ["permit_id"], ["permits.id"], name=op.f("fk_inspection_acts_permit_id_permits")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["inspection_tasks.id"],
            name=op.f("fk_inspection_acts_task_id_inspection_tasks"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inspection_acts")),
    )
    op.create_geospatial_index(  # pyright: ignore[reportAttributeAccessIssue]
        "idx_inspection_acts_gps",
        "inspection_acts",
        ["gps"],
        unique=False,
        postgresql_using="gist",
        postgresql_ops={},
    )
    op.create_index(
        op.f("ix_inspection_acts_application_id"),
        "inspection_acts",
        ["application_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_inspection_acts_checklist_id"), "inspection_acts", ["checklist_id"], unique=False
    )
    op.create_index(
        op.f("ix_inspection_acts_inspector_id"), "inspection_acts", ["inspector_id"], unique=False
    )
    op.create_index(
        op.f("ix_inspection_acts_organization_id"),
        "inspection_acts",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_inspection_acts_permit_id"), "inspection_acts", ["permit_id"], unique=False
    )
    op.create_index(
        op.f("ix_inspection_acts_task_id"), "inspection_acts", ["task_id"], unique=False
    )
    op.create_table(
        "inspection_act_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("act_id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('photo', 'video')", name=op.f("ck_inspection_act_files_kind_valid")
        ),
        sa.ForeignKeyConstraint(
            ["act_id"],
            ["inspection_acts.id"],
            name=op.f("fk_inspection_act_files_act_id_inspection_acts"),
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["media_files.id"],
            name=op.f("fk_inspection_act_files_file_id_media_files"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inspection_act_files")),
        sa.UniqueConstraint("act_id", "file_id", name="uq_inspection_act_files_act_file"),
    )
    op.create_index(
        op.f("ix_inspection_act_files_act_id"), "inspection_act_files", ["act_id"], unique=False
    )
    op.create_index(
        op.f("ix_inspection_act_files_file_id"), "inspection_act_files", ["file_id"], unique=False
    )
    op.create_table(
        "violation_cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("act_id", sa.Uuid(), nullable=False),
        sa.Column("permit_id", sa.Uuid(), nullable=True),
        sa.Column("applicant_id", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("violation_type_item_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("explanation_due_at", sa.Date(), nullable=True),
        sa.Column("explanation_text", sa.Text(), nullable=True),
        sa.Column("explanation_file_id", sa.Uuid(), nullable=True),
        sa.Column("damage_amount", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("damage_calc", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("decision_due_at", sa.Date(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
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
            "decision IS NULL OR decision IN ('warning', 'suspend', 'revoke', 'transfer')",
            name=op.f("ck_violation_cases_decision_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('opened', 'explanation_requested', 'explained', 'decided',"
            " 'appealed', 'closed', 'archived')",
            name=op.f("ck_violation_cases_status_valid"),
        ),
        sa.CheckConstraint(
            "damage_amount IS NULL OR damage_amount >= 0",
            name=op.f("ck_violation_cases_damage_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["act_id"],
            ["inspection_acts.id"],
            name=op.f("fk_violation_cases_act_id_inspection_acts"),
        ),
        sa.ForeignKeyConstraint(
            ["applicant_id"],
            ["applicants.id"],
            name=op.f("fk_violation_cases_applicant_id_applicants"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name=op.f("fk_violation_cases_decided_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["explanation_file_id"],
            ["media_files.id"],
            name=op.f("fk_violation_cases_explanation_file_id_media_files"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_violation_cases_organization_id_organizations"),
        ),
        sa.ForeignKeyConstraint(
            ["permit_id"], ["permits.id"], name=op.f("fk_violation_cases_permit_id_permits")
        ),
        sa.ForeignKeyConstraint(
            ["violation_type_item_id"],
            ["classifier_items.id"],
            name=op.f("fk_violation_cases_violation_type_item_id_classifier_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_violation_cases")),
        sa.UniqueConstraint("number", name=op.f("uq_violation_cases_number")),
    )
    op.create_index(op.f("ix_violation_cases_act_id"), "violation_cases", ["act_id"], unique=False)
    op.create_index(
        op.f("ix_violation_cases_applicant_id"), "violation_cases", ["applicant_id"], unique=False
    )
    op.create_index(
        op.f("ix_violation_cases_organization_id"),
        "violation_cases",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_violation_cases_permit_id"), "violation_cases", ["permit_id"], unique=False
    )
    op.create_index(
        op.f("ix_violation_cases_violation_type_item_id"),
        "violation_cases",
        ["violation_type_item_id"],
        unique=False,
    )
    op.create_table(
        "violation_appeals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("filed_by", sa.Uuid(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["violation_cases.id"],
            name=op.f("fk_violation_appeals_case_id_violation_cases"),
        ),
        sa.ForeignKeyConstraint(
            ["filed_by"], ["users.id"], name=op.f("fk_violation_appeals_filed_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"], ["users.id"], name=op.f("fk_violation_appeals_resolved_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_violation_appeals")),
    )
    op.create_index(
        op.f("ix_violation_appeals_case_id"), "violation_appeals", ["case_id"], unique=False
    )
    op.create_table(
        "violation_case_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["violation_cases.id"],
            name=op.f("fk_violation_case_history_case_id_violation_cases"),
        ),
        sa.ForeignKeyConstraint(
            ["changed_by"], ["users.id"], name=op.f("fk_violation_case_history_changed_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_violation_case_history")),
    )
    op.create_index(
        op.f("ix_violation_case_history_case_id"),
        "violation_case_history",
        ["case_id"],
        unique=False,
    )
    op.create_index(
        "ix_violation_case_history_timeline",
        "violation_case_history",
        ["case_id", "occurred_at", "id"],
        unique=False,
    )
    # ### end Alembic commands ###

    # violation_case_history is append-only, mirroring audit_log's (0002),
    # calculations's (0011), application_status_history's and
    # permit_status_history's (0015, 0019) own triggers exactly.
    op.execute(
        """
        CREATE FUNCTION violation_case_history_forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'violation_case_history is append-only: a correction is a new row';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER violation_case_history_append_only "
        "BEFORE UPDATE OR DELETE ON violation_case_history "
        "FOR EACH ROW EXECUTE FUNCTION violation_case_history_forbid_mutation()"
    )
    op.execute(
        "CREATE TRIGGER violation_case_history_no_truncate "
        "BEFORE TRUNCATE ON violation_case_history "
        "FOR EACH STATEMENT EXECUTE FUNCTION violation_case_history_forbid_mutation()"
    )

    # Reference seeds: six items on the (already-existing, empty) violation_types
    # classifier, one default checklist, this module's five permission codes
    # granted to their roles (plus the `inspector` -> `permits.view_any` fix —
    # see this file's own docstring). The classifier lookup mirrors
    # `0024_benefit_proof_doc_type.py`'s own defensive shape exactly: resolve
    # the id (raising by name if the chain this revision depends on never ran),
    # insert `ON CONFLICT DO NOTHING` against the partial unique index, verify
    # the count afterward so "nothing was inserted" can never pass as "seeded".
    conn = op.get_bind()
    classifier_id = conn.execute(
        sa.text("SELECT id FROM classifiers WHERE code = :code").bindparams(code=CLASSIFIER_CODE)
    ).scalar()
    if classifier_id is None:
        raise RuntimeError(
            f"classifier {CLASSIFIER_CODE!r} is missing — 0005_admin_seeds.py seeds it, so this "
            "database did not run the chain this revision depends on"
        )
    for code, item_id, name_cyr, name_ru, sort_order in VIOLATION_TYPES:
        conn.execute(
            sa.text(
                "INSERT INTO classifier_items "
                "(id, classifier_id, code, name, valid_from, sort_order, status) VALUES "
                "(CAST(:id AS uuid), CAST(:classifier_id AS uuid), :code, "
                "jsonb_build_object('uz_cyrl', :cyr, 'ru', :ru), "
                "DATE '2026-01-01', :sort, 'active') "
                "ON CONFLICT (classifier_id, code) WHERE status = 'active' DO NOTHING"
            ).bindparams(
                id=item_id,
                classifier_id=classifier_id,
                code=code,
                cyr=name_cyr,
                ru=name_ru,
                sort=sort_order,
            )
        )
    seeded = conn.execute(
        sa.text(
            "SELECT count(*) FROM classifier_items "
            "WHERE classifier_id = CAST(:classifier_id AS uuid) AND status = 'active' "
            "AND code = ANY(:codes)"
        ).bindparams(classifier_id=classifier_id, codes=[c for c, *_ in VIOLATION_TYPES])
    ).scalar()
    if seeded != len(VIOLATION_TYPES):
        raise RuntimeError(
            f"expected {len(VIOLATION_TYPES)} active violation_types items, found {seeded} — "
            "some insert was skipped by ON CONFLICT with nothing to conflict with"
        )

    op.execute(
        sa.text(
            "INSERT INTO checklists (id, code, version, name, activity_type_id, items, status) "
            "VALUES (CAST(:id AS uuid), :code, 1, "
            "jsonb_build_object('uz_cyrl', :cyr, 'ru', :ru), NULL, CAST(:items AS jsonb), 'active')"
        ).bindparams(
            id=CHECKLIST_ID,
            code=CHECKLIST_CODE,
            cyr="Стандарт далада текшириш чек-листи",
            ru="Стандартный чек-лист полевой инспекции",
            items=json.dumps(CHECKLIST_ITEMS),
        )
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
    # Seeds first, in reverse order (lesson: a downgrade must delete whatever
    # its upgrade made possible).
    for role, code in ROLE_GRANTS:
        op.execute(
            sa.text(
                "DELETE FROM role_permissions WHERE permission_code = :code "
                "AND role_id = (SELECT id FROM roles WHERE code = :role)"
            ).bindparams(code=code, role=role)
        )
    op.execute(
        sa.text("DELETE FROM checklists WHERE id = CAST(:id AS uuid)").bindparams(id=CHECKLIST_ID)
    )
    # By ID, one at a time, never by classifier_id: `0005_admin_seeds.py` owns
    # the classifier itself (and may hold admin-created items beside ours by
    # the time this downgrade runs) — this migration owns only the six rows it
    # inserted (the same per-row shape `0024_benefit_proof_doc_type.py` uses).
    for _code, item_id, *_rest in VIOLATION_TYPES:
        op.execute(
            sa.text("DELETE FROM classifier_items WHERE id = CAST(:id AS uuid)").bindparams(
                id=item_id
            )
        )

    op.execute(
        "DROP TRIGGER IF EXISTS violation_case_history_no_truncate ON violation_case_history"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS violation_case_history_append_only ON violation_case_history"
    )
    op.execute("DROP FUNCTION IF EXISTS violation_case_history_forbid_mutation()")

    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index("ix_violation_case_history_timeline", table_name="violation_case_history")
    op.drop_index(op.f("ix_violation_case_history_case_id"), table_name="violation_case_history")
    op.drop_table("violation_case_history")
    op.drop_index(op.f("ix_violation_appeals_case_id"), table_name="violation_appeals")
    op.drop_table("violation_appeals")
    op.drop_index(op.f("ix_violation_cases_violation_type_item_id"), table_name="violation_cases")
    op.drop_index(op.f("ix_violation_cases_permit_id"), table_name="violation_cases")
    op.drop_index(op.f("ix_violation_cases_organization_id"), table_name="violation_cases")
    op.drop_index(op.f("ix_violation_cases_applicant_id"), table_name="violation_cases")
    op.drop_index(op.f("ix_violation_cases_act_id"), table_name="violation_cases")
    op.drop_table("violation_cases")
    op.drop_index(op.f("ix_inspection_act_files_file_id"), table_name="inspection_act_files")
    op.drop_index(op.f("ix_inspection_act_files_act_id"), table_name="inspection_act_files")
    op.drop_table("inspection_act_files")
    op.drop_index(op.f("ix_inspection_acts_task_id"), table_name="inspection_acts")
    op.drop_index(op.f("ix_inspection_acts_permit_id"), table_name="inspection_acts")
    op.drop_index(op.f("ix_inspection_acts_organization_id"), table_name="inspection_acts")
    op.drop_index(op.f("ix_inspection_acts_inspector_id"), table_name="inspection_acts")
    op.drop_index(op.f("ix_inspection_acts_checklist_id"), table_name="inspection_acts")
    op.drop_index(op.f("ix_inspection_acts_application_id"), table_name="inspection_acts")
    op.drop_geospatial_index(  # pyright: ignore[reportAttributeAccessIssue]
        "idx_inspection_acts_gps",
        table_name="inspection_acts",
        postgresql_using="gist",
        column_name="gps",
    )
    op.drop_geospatial_table("inspection_acts")  # pyright: ignore[reportAttributeAccessIssue]
    op.drop_index(op.f("ix_inspection_tasks_permit_id"), table_name="inspection_tasks")
    op.drop_index(op.f("ix_inspection_tasks_organization_id"), table_name="inspection_tasks")
    op.drop_index(op.f("ix_inspection_tasks_contour_id"), table_name="inspection_tasks")
    op.drop_index(op.f("ix_inspection_tasks_assigned_to"), table_name="inspection_tasks")
    op.drop_index(op.f("ix_inspection_tasks_application_id"), table_name="inspection_tasks")
    op.drop_table("inspection_tasks")
    op.drop_index(
        "uq_checklists_active_code",
        table_name="checklists",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_index(op.f("ix_checklists_activity_type_id"), table_name="checklists")
    op.drop_table("checklists")
    # ### end Alembic commands ###
