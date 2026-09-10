"""beekeepers and the benefit list

Stage 10, rulings #181/#182 (docs/decisions.md; wave-1 track B3). Four concerns
in one migration because they are one fact — "the benefit list, and who keeps
its register":

  (a) `beekeepers`: the Beekeeping Union's own register of certificate holders
      — `beekeepers.service.match_certificate`'s ONE data source. Rows are
      never deleted (design/02 principle 7): removing a member sets
      `status='removed'` with a mandatory `removed_reason`, and
      `uq_beekeepers_certificate_no_active` is a PARTIAL unique index
      (`status='active'` only, the same idiom `0005`'s own
      `uq_classifier_items_active_code` uses) so a removed row never blocks a
      future re-registration under the same number.

  (b) Role `benefit_verifier` -> `beekeeping_registrar`, SAME id
      (`0198f000-0000-7000-8000-00000000000c`, minted by `0051`) — only
      `code`/`name` change, so a user already assigned that role keeps it
      across this migration with no `users.role_id` rewrite at all. Ruling
      #182: "The central role of #179 is not deleted, it is renamed to what
      it now is."

  (c) Permission `beekeepers.manage`, granted to the renamed role.
      `benefits.verify` is REVOKED from it (the central office no longer
      verifies claims — the leshoz does, "inside the review") and GRANTED to
      `executor_staff`/`executor_head` instead (ruling #182). Seeded the way
      `0015`/`0051` seed `ROLE_GRANTS` — `INSERT ... SELECT id FROM roles
      WHERE code = ... ON CONFLICT DO NOTHING`.

      `app/modules/applications/permissions.py` still narrates the OLD split
      (`benefit_verifier` alone holding `benefits.verify`) as of this
      migration — wave-2's track (`applications`) updates that file and its
      own tests in the same stage; this migration's file ownership stops at
      `app/modules/beekeepers/**`, the demo seed and this file.

  (d) The seven `benefit_categories` classifier items of ruling #181 (VMQ 278
      §IV ¶12; PQ-3327 ¶8) — `beekeeping_union_member` on `apiary`, the other
      six on `recreation`. Every one of them is a lawful 100% modifier
      (`tariffs.benefit_modifiers`, seeded onto the DEMO tariffs by
      `app/seed/demo.py`, not here — a migration ships the DICTIONARY entry a
      claim can reference, not a specific tariff's data). `requires_certificate`
      is deliberately NOT written into `props`: ruling #181 makes the
      certificate number mandatory for every category, not a per-item switch,
      so `applications.service._open_benefit_verification` stops reading that
      key (wave-2's own change, not this migration's). Seeded
      `ON CONFLICT DO NOTHING` on `(classifier_id, code) WHERE status =
      'active'`, the same idempotent shape `0024` uses for `benefit_proof`.

Downgrade reverses (b) and (c) exactly back to `0051`'s own post-state, and
(a) drops the table. (d) is also reversed — the seven items are DELETED, not
left in place: unlike `0024`'s `benefit_proof` (a document TYPE nothing else
in this migration created), an application referencing one of these seven
codes would silently misprice under `_check_benefit_claim`'s
`unknown_benefit_code` refusal the moment the category it names no longer
exists in the classifier that this same migration is undoing — leaving stale
dictionary data behind while removing the role/permission machinery that
verifies it would be a worse inconsistency than removing both together.
`applications.benefit_category_item_id` is nullable and the table is not
append-only, so the downgrade nulls any reference before deleting the row
(the same "a downgrade must delete whatever its upgrade made possible"
lesson `0010`/`0023` already pay for elsewhere) — empty on every database this
round-trip actually runs against, since nothing before this migration could
have referenced a code this migration itself introduces.

Revision ID: 0053
Revises: 0051
Create Date: 2026-09-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0053"
down_revision: str | Sequence[str] | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- (b) role rename ---------------------------------------------------------------

# `0051`'s own id — unchanged, so a user already holding this role keeps it.
BEEKEEPING_REGISTRAR_ROLE_ID = "0198f000-0000-7000-8000-00000000000c"
OLD_ROLE_CODE = "benefit_verifier"
NEW_ROLE_CODE = "beekeeping_registrar"

# `0051`'s own seeded name — restored verbatim by the downgrade.
OLD_ROLE_NAME = {
    "uz_latn": "Imtiyoz sertifikatlarini tekshiruvchi",
    "uz_cyrl": "Имтиёз сертификатларини текширувчи",
    "ru": "Проверяющий льготных сертификатов",
    "en": "Benefit certificate verifier",
}
NEW_ROLE_NAME = {
    "uz_latn": "Asalarichilar uyushmasi xodimi",
    "uz_cyrl": "Асаларичилар уюшмаси ходими",
    "ru": "Сотрудник Союза пчеловодов",
    "en": "Beekeeping Union registrar",
}

# --- (c) permission grants ----------------------------------------------------------

BEEKEEPERS_MANAGE_CODE = "beekeepers.manage"
BENEFITS_VERIFY_CODE = "benefits.verify"
BENEFITS_VERIFY_NEW_ROLES = ("executor_staff", "executor_head")

# --- (d) benefit_categories classifier items -----------------------------------------

BENEFIT_CATEGORIES_CLASSIFIER_CODE = "benefit_categories"
VMQ_278 = "ВМҚ 278-сон, 30.09.2015, §IV ¶12"
PQ_3327 = "ПҚ-3327-сон, ¶8"

# (code, uz_latn, uz_cyrl, ru, activity, basis, sort_order)
BENEFIT_CATEGORIES: list[tuple[str, str, str, str, str, str, int]] = [
    (
        "beekeeping_union_member",
        "Asalarichilar uyushmasi a'zosi",
        "Асаларичилар уюшмаси аъзоси",
        "Член Союза пчеловодов",
        "apiary",
        PQ_3327,
        10,
    ),
    (
        "preschool_children",
        "Maktabgacha yoshdagi bolalar",
        "Мактабгача ёшдаги болалар",
        "Дети дошкольного возраста",
        "recreation",
        VMQ_278,
        20,
    ),
    (
        "education_institutions",
        "O'quvchilar, talabalar va ularning o'qituvchilari",
        "Ўқувчилар, талабалар ва уларнинг ўқитувчилари",
        "Учащиеся, студенты и их преподаватели",
        "recreation",
        VMQ_278,
        30,
    ),
    (
        "orphanage_residents",
        '"Mehribonlik" va "Muruvvat" tarbiyalanuvchilari',
        "«Меҳрибонлик» ва «Мурувват» тарбияланувчилари",
        "Воспитанники «Мехрибонлик» и «Мурувват»",
        "recreation",
        VMQ_278,
        40,
    ),
    (
        "persons_with_disabilities",
        "Nogironligi bo'lgan shaxslar",
        "Ногиронлиги бўлган шахслар",
        "Инвалиды",
        "recreation",
        VMQ_278,
        50,
    ),
    (
        "war_veterans",
        "1941-1945-yillar urushi ishtirokchilari, nogironlari va ularga tenglashtirilgan shaxslar",
        "1941-1945 йиллар уруши иштирокчилари, ногиронлари ва уларга тенглаштирилган шахслар",
        "Участники и инвалиды войны 1941-1945 годов и приравненные к ним лица",
        "recreation",
        VMQ_278,
        60,
    ),
    (
        "radiation_victims",
        "Radiatsiya ta'siridan jabrlanganlar",
        "Радиация таъсиридан жабрланганлар",
        "Лица, пострадавшие от радиации",
        "recreation",
        VMQ_278,
        70,
    ),
]

# Fixed ids (0024's own convention: migration number embedded, sequential
# tail) — so the downgrade can name exactly the rows this migration wrote,
# never a code-based DELETE that might remove an admin-created row instead.
BENEFIT_CATEGORY_IDS = [
    f"0198f100-0053-7000-8000-{i:012d}" for i in range(1, len(BENEFIT_CATEGORIES) + 1)
]


def upgrade() -> None:
    """Upgrade schema."""
    # --- (a) beekeepers table --------------------------------------------------
    op.create_table(
        "beekeepers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("certificate_no", sa.Text(), nullable=False),
        sa.Column("pinfl", sa.Text(), nullable=False),
        sa.Column("passport_series", sa.Text(), nullable=False),
        sa.Column("passport_number", sa.Text(), nullable=False),
        sa.Column("stir", sa.Text(), nullable=True),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("farm_name", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("removed_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
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
        sa.CheckConstraint(r"pinfl ~ '^[0-9]{14}$'", name=op.f("ck_beekeepers_pinfl_format")),
        sa.CheckConstraint(
            "status <> 'removed' OR "
            "(removed_reason IS NOT NULL AND length(trim(removed_reason)) > 0)",
            name=op.f("ck_beekeepers_removed_reason_required"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'removed')", name=op.f("ck_beekeepers_status_valid")
        ),
        sa.CheckConstraint(
            r"stir IS NULL OR stir ~ '^[0-9]{9}$'", name=op.f("ck_beekeepers_stir_format")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_beekeepers_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name=op.f("fk_beekeepers_updated_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_beekeepers")),
    )
    op.create_index("ix_beekeepers_pinfl", "beekeepers", ["pinfl"], unique=False)
    op.create_index(
        "uq_beekeepers_certificate_no_active",
        "beekeepers",
        ["certificate_no"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # --- (b) role rename ---------------------------------------------------------
    result = op.get_bind().execute(
        sa.text(
            "UPDATE roles SET code = :new_code, "
            "name = jsonb_build_object('uz_latn', :latn, 'uz_cyrl', :cyr, 'ru', :ru, 'en', :en) "
            "WHERE id = CAST(:id AS uuid) AND code = :old_code"
        ).bindparams(
            id=BEEKEEPING_REGISTRAR_ROLE_ID,
            old_code=OLD_ROLE_CODE,
            new_code=NEW_ROLE_CODE,
            latn=NEW_ROLE_NAME["uz_latn"],
            cyr=NEW_ROLE_NAME["uz_cyrl"],
            ru=NEW_ROLE_NAME["ru"],
            en=NEW_ROLE_NAME["en"],
        )
    )
    if result.rowcount != 1:
        raise RuntimeError(
            f"expected to rename exactly one role ({OLD_ROLE_CODE!r} -> {NEW_ROLE_CODE!r}, "
            f"id {BEEKEEPING_REGISTRAR_ROLE_ID}), affected {result.rowcount} — this database "
            "did not run 0051, or something already renamed it"
        )

    # --- (c) permission grants ----------------------------------------------------
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_code) "
            "SELECT id, :code FROM roles WHERE code = :role "
            "ON CONFLICT DO NOTHING"
        ).bindparams(code=BEEKEEPERS_MANAGE_CODE, role=NEW_ROLE_CODE)
    )
    op.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_code = :code "
            "AND role_id = (SELECT id FROM roles WHERE code = :role)"
        ).bindparams(code=BENEFITS_VERIFY_CODE, role=NEW_ROLE_CODE)
    )
    for role in BENEFITS_VERIFY_NEW_ROLES:
        op.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_code) "
                "SELECT id, :code FROM roles WHERE code = :role "
                "ON CONFLICT DO NOTHING"
            ).bindparams(code=BENEFITS_VERIFY_CODE, role=role)
        )

    # --- (d) benefit_categories classifier items -----------------------------------
    conn = op.get_bind()
    classifier_id = conn.execute(
        sa.text("SELECT id FROM classifiers WHERE code = :code").bindparams(
            code=BENEFIT_CATEGORIES_CLASSIFIER_CODE
        )
    ).scalar()
    if classifier_id is None:
        raise RuntimeError(
            f"classifier {BENEFIT_CATEGORIES_CLASSIFIER_CODE!r} is missing — 0005_admin_seeds.py "
            "seeds it, so this database did not run the chain this revision depends on"
        )
    for item_id, (code, latn, cyr, ru, activity, basis, sort_order) in zip(
        BENEFIT_CATEGORY_IDS, BENEFIT_CATEGORIES, strict=True
    ):
        conn.execute(
            sa.text(
                "INSERT INTO classifier_items "
                "(id, classifier_id, code, name, props, valid_from, sort_order, status) VALUES "
                "(CAST(:id AS uuid), CAST(:classifier_id AS uuid), :code, "
                "jsonb_build_object('uz_latn', :latn, 'uz_cyrl', :cyr, 'ru', :ru), "
                "jsonb_build_object('activity', :activity, 'basis', :basis), "
                "DATE '2026-01-01', :sort_order, 'active') "
                # The index is PARTIAL, so the conflict target must repeat its own
                # WHERE clause or Postgres cannot infer it (0024's own note).
                "ON CONFLICT (classifier_id, code) WHERE status = 'active' DO NOTHING"
            ).bindparams(
                id=item_id,
                classifier_id=classifier_id,
                code=code,
                latn=latn,
                cyr=cyr,
                ru=ru,
                activity=activity,
                basis=basis,
                sort_order=sort_order,
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    # --- (d) benefit_categories classifier items -----------------------------------
    for item_id in BENEFIT_CATEGORY_IDS:
        # Nullable, non-append-only referrer: null the reference before the delete
        # (lesson: "a downgrade must delete whatever its upgrade made possible").
        op.execute(
            sa.text(
                "UPDATE applications SET benefit_category_item_id = NULL "
                "WHERE benefit_category_item_id = CAST(:id AS uuid)"
            ).bindparams(id=item_id)
        )
        op.execute(
            sa.text("DELETE FROM classifier_items WHERE id = CAST(:id AS uuid)").bindparams(
                id=item_id
            )
        )

    # --- (c) permission grants ----------------------------------------------------
    for role in BENEFITS_VERIFY_NEW_ROLES:
        op.execute(
            sa.text(
                "DELETE FROM role_permissions WHERE permission_code = :code "
                "AND role_id = (SELECT id FROM roles WHERE code = :role)"
            ).bindparams(code=BENEFITS_VERIFY_CODE, role=role)
        )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_code) "
            "SELECT id, :code FROM roles WHERE code = :role "
            "ON CONFLICT DO NOTHING"
        ).bindparams(code=BENEFITS_VERIFY_CODE, role=NEW_ROLE_CODE)
    )
    op.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_code = :code "
            "AND role_id = (SELECT id FROM roles WHERE code = :role)"
        ).bindparams(code=BEEKEEPERS_MANAGE_CODE, role=NEW_ROLE_CODE)
    )

    # --- (b) role rename, reversed -------------------------------------------------
    result = op.get_bind().execute(
        sa.text(
            "UPDATE roles SET code = :old_code, "
            "name = jsonb_build_object('uz_latn', :latn, 'uz_cyrl', :cyr, 'ru', :ru, 'en', :en) "
            "WHERE id = CAST(:id AS uuid) AND code = :new_code"
        ).bindparams(
            id=BEEKEEPING_REGISTRAR_ROLE_ID,
            old_code=OLD_ROLE_CODE,
            new_code=NEW_ROLE_CODE,
            latn=OLD_ROLE_NAME["uz_latn"],
            cyr=OLD_ROLE_NAME["uz_cyrl"],
            ru=OLD_ROLE_NAME["ru"],
            en=OLD_ROLE_NAME["en"],
        )
    )
    if result.rowcount != 1:
        raise RuntimeError(
            f"expected to rename exactly one role ({NEW_ROLE_CODE!r} -> {OLD_ROLE_CODE!r}, "
            f"id {BEEKEEPING_REGISTRAR_ROLE_ID}), affected {result.rowcount}"
        )

    # --- (a) beekeepers table, dropped ----------------------------------------------
    op.drop_index(
        "uq_beekeepers_certificate_no_active",
        table_name="beekeepers",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_index("ix_beekeepers_pinfl", table_name="beekeepers")
    op.drop_table("beekeepers")
