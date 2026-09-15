"""permit_blanks

Stage 15 (decisions #213, #214, #215). Three things, one revision:

1. Four nullable columns on `applications` — the lines of the deadwood and
   recreation blanks the wizard did not collect (R6). CHECK-backed codes.
2. `science` is ARCHIVED, not deleted (#214, R7): there is no blank for it and
   no admin route writes `activity_types`, so this is the one path that reaches
   every environment identically. `admin.repo.list_activity_types` lists active
   rows only and `applications.service._assert_references` refuses an archived
   activity, so the wizard's menu and the API both stop offering it here.
3. `permit_templates` is SUPERSEDED for the five open activities (R1; 3.11a
   ruling 14 — a new layout is a new version, never a rewrite): every active row
   is archived and a new row with `layout_file_id NULL` ("the bundled blank of
   this activity", `permits.render.bundled_layout`) is inserted at `max(version)
   + 1`. Until this revision only grazing had an active row, so `issue()` refused
   every other activity with `no_active_template` — this closes that.

   Archive FIRST, then insert: `uq_permit_templates_active` is a partial unique
   index over `status = 'active'`, and a test database may already hold an
   active apiary row left by `tests/modules/permits/conftest.py::apiary_template`.

   The insert is idempotent (`ON CONFLICT (id) DO UPDATE`): a downgrade that kept
   one of these rows (below) leaves its fixed id in place, and a re-upgrade must
   reactivate that same row rather than collide with it on the primary key.

`downgrade()`'s own rule for the five rows this revision seeds: a `permits` row
may already reference one by the time downgrade runs (a permit does not stop
existing because the revision that seeded its layout is reversed — the same
"keep it" reasoning `tz/05` invariant 7 applies to every other frozen permit
field). So each of the five is ARCHIVED, never deleted, when `permits.template_id`
still points at it; only a row nothing references is deleted, as before.
`fk_permits_template_id_permit_templates` is what would otherwise abort the
whole downgrade — found the hard way, by `pytest -n4 --fresh-db`'s full-suite
order issuing a non-grazing permit before `test_downgrade_upgrade_roundtrip` ran.

Revision ID: 0061
Revises: 0063
Create Date: 2026-09-14 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0061"
down_revision: str | Sequence[str] | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `activity_types.id` as migration 0005 fixed them — a migration is frozen
# history and repeats literals rather than importing the app.
ACTIVITY_IDS = {
    "grazing": "0198f100-0001-7000-8000-000000000001",
    "haymaking": "0198f100-0001-7000-8000-000000000002",
    "apiary": "0198f100-0001-7000-8000-000000000003",
    "recreation": "0198f100-0001-7000-8000-000000000004",
    "deadwood": "0198f100-0001-7000-8000-000000000005",
}
SCIENCE_ID = "0198f100-0001-7000-8000-000000000006"

# (code, template id, uz_latn, uz_cyrl, ru) — the blank's own title.
TEMPLATES = (
    (
        "grazing",
        "0198f150-0015-7000-8000-000000000001",
        "Ruxsatnoma — chorva mollarini boqish (rasmiy blank)",
        "Рухсатнома — чорва молларини боқиш (расмий бланк)",
        "Разрешение — выпас скота (официальный бланк)",
    ),
    (
        "haymaking",
        "0198f150-0015-7000-8000-000000000002",
        "Ruxsatnoma — pichan o‘rish (rasmiy blank)",
        "Рухсатнома — пичан ўриш (расмий бланк)",
        "Разрешение — сенокошение (официальный бланк)",
    ),
    (
        "apiary",
        "0198f150-0015-7000-8000-000000000003",
        "Ruxsatnoma — asalari uyalarini joylashtirish (rasmiy blank)",
        "Рухсатнома — асалари уяларини жойлаштириш (расмий бланк)",
        "Разрешение — размещение пчелиных ульев (официальный бланк)",
    ),
    (
        "deadwood",
        "0198f150-0015-7000-8000-000000000004",
        "Ruxsatnoma — o‘tin va shox-shabba yig‘ish (rasmiy blank)",
        "Рухсатнома — ўтин ва шох-шабба йиғиш (расмий бланк)",
        "Разрешение — сбор дров и ветвей (официальный бланк)",
    ),
    (
        "recreation",
        "0198f150-0015-7000-8000-000000000005",
        "Ruxsatnoma — rekreatsion foydalanish (rasmiy blank)",
        "Рухсатнома — рекреацион фойдаланиш (расмий бланк)",
        "Разрешение — рекреационное использование (официальный бланк)",
    ),
)

DEADWOOD_PRODUCTS = ("firewood", "branches", "both")
RECREATION_PURPOSES = ("cultural_educational", "upbringing", "health", "recreational", "aesthetic")


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    # 1. The four blank fields (R6). `sa.Text()`, not `sa.String()`: `Base.type_annotation_map`
    # sends plain `str` to TEXT (decision #27), and `Mapped[str | None]` on the model gives
    # `test_autogenerate_diff_empty` a TEXT column to compare this migration against.
    op.add_column("applications", sa.Column("deadwood_product", sa.Text(), nullable=True))
    op.add_column("applications", sa.Column("removal_deadline", sa.Date(), nullable=True))
    op.add_column("applications", sa.Column("recreation_purpose", sa.Text(), nullable=True))
    op.add_column("applications", sa.Column("event_at", sa.DateTime(timezone=True), nullable=True))
    # Short names here, not the fully-qualified "ck_applications_..." — the naming
    # convention (`app/db.py::NAMING_CONVENTION`) adds that prefix itself, and
    # passing it already-qualified doubles it (0038/0047/0049's own note).
    op.create_check_constraint(
        "deadwood_product_valid",
        "applications",
        f"deadwood_product IS NULL OR deadwood_product IN ({_in_list(DEADWOOD_PRODUCTS)})",
    )
    op.create_check_constraint(
        "recreation_purpose_valid",
        "applications",
        f"recreation_purpose IS NULL OR recreation_purpose IN ({_in_list(RECREATION_PURPOSES)})",
    )

    # 2. Science off the menu (#214, R7).
    op.execute(
        sa.text(
            "UPDATE activity_types SET status = 'archived' WHERE id = CAST(:id AS uuid)"
        ).bindparams(id=SCIENCE_ID)
    )

    # 3. The blanks (R1).
    for code, template_id, latn, cyrl, ru in TEMPLATES:
        activity_id = ACTIVITY_IDS[code]
        op.execute(
            sa.text(
                "UPDATE permit_templates SET status = 'archived' "
                "WHERE activity_type_id = CAST(:activity AS uuid) AND status = 'active'"
            ).bindparams(activity=activity_id)
        )
        op.execute(
            sa.text(
                "INSERT INTO permit_templates "
                "(id, activity_type_id, version, name, layout_file_id, status, valid_from) "
                "SELECT CAST(:id AS uuid), CAST(:activity AS uuid), "
                "COALESCE(MAX(version), 0) + 1, "
                "jsonb_build_object('uz_latn', :latn, 'uz_cyrl', :cyrl, 'ru', :ru), "
                "NULL, 'active', DATE '2026-09-14' "
                "FROM permit_templates WHERE activity_type_id = CAST(:activity AS uuid) "
                # A re-upgrade after a downgrade that ARCHIVED (not deleted) this
                # same fixed id must reactivate it, not collide with it on the PK —
                # `version` stays whatever the row already carries, never the
                # `COALESCE(MAX(version), 0) + 1` the SELECT computed for a fresh
                # insert (that count now includes this very row).
                "ON CONFLICT (id) DO UPDATE SET status = 'active', name = EXCLUDED.name"
            ).bindparams(id=template_id, activity=activity_id, latn=latn, cyrl=cyrl, ru=ru)
        )


def downgrade() -> None:
    # The blanks: for each of the five rows this revision inserted, ARCHIVE it
    # (never delete) if a `permits` row still references it — a permit keeps
    # pointing at the layout it was issued with, the same "frozen at issuance"
    # reasoning every other permit field already gets — and DELETE it otherwise,
    # as before. Then put 0019's grazing version 1 back in force; other
    # activities had no active row before this revision (any archived row left
    # over is a test fixture's own), so nothing to restore there.
    bind = op.get_bind()
    for _code, template_id, *_ in TEMPLATES:
        in_use = bind.execute(
            sa.text(
                "SELECT 1 FROM permits WHERE template_id = CAST(:id AS uuid) LIMIT 1"
            ).bindparams(id=template_id)
        ).first()
        if in_use is not None:
            op.execute(
                sa.text(
                    "UPDATE permit_templates SET status = 'archived' WHERE id = CAST(:id AS uuid)"
                ).bindparams(id=template_id)
            )
        else:
            op.execute(
                sa.text("DELETE FROM permit_templates WHERE id = CAST(:id AS uuid)").bindparams(
                    id=template_id
                )
            )
    op.execute(
        sa.text(
            "UPDATE permit_templates SET status = 'active' "
            "WHERE activity_type_id = CAST(:activity AS uuid) AND version = 1"
        ).bindparams(activity=ACTIVITY_IDS["grazing"])
    )
    op.execute(
        sa.text(
            "UPDATE activity_types SET status = 'active' WHERE id = CAST(:id AS uuid)"
        ).bindparams(id=SCIENCE_ID)
    )
    op.drop_constraint("recreation_purpose_valid", "applications", type_="check")
    op.drop_constraint("deadwood_product_valid", "applications", type_="check")
    op.drop_column("applications", "event_at")
    op.drop_column("applications", "recreation_purpose")
    op.drop_column("applications", "removal_deadline")
    op.drop_column("applications", "deadwood_product")
