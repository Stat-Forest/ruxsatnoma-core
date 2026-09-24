"""application printouts

Stage 16 task B2 (rulings R3/R4/R6/R7/R12). Two tables and their seed data, one
revision:

1. `application_rejection_grounds` — one row per ground of a rejection decision
   (1..10, ruling R3), append-only via the same BEFORE UPDATE/DELETE/TRUNCATE
   idiom as `application_status_history` (migration 0015) and `calculations`
   (migration 0011): a decision's own grounds must never change once the head
   has signed over the package that names them.
2. `application_printouts` — the application letter and the rejection notice
   (ruling R6): the `snapshot` is frozen at recording time (a second freeze
   trigger forbids changing it, or the row's identity/kind/submission/number/
   language/timestamp), while `file_id`/`sha256` start NULL and may move to a
   value EXACTLY ONCE, on the first download (lazy render). `kind='letter'`
   rows always carry a `submission_id` (one letter per submission, ruling R7);
   `kind='rejection_notice'` rows always carry a `number` and there may be at
   most one per application (`uq_application_printouts_one_notice`).
3. Seed data for ruling R4: eight new rejection codes R01..R08 under the
   existing `rejection_reasons` classifier (migration 0005), `kind='reject'`;
   RJ-03..RJ-12 (the old refusal codes) are ARCHIVED, never deleted — reference
   data is superseded, not removed (`CLAUDE.md`); RJ-15 becomes return-only
   (`kind` "both" -> "return"). RJ-01/RJ-02 (return) and RJ-13/RJ-14 (cancel)
   are untouched.

Revision ID: 0064
Revises: 0061
Create Date: 2026-09-24 18:27:54.139608

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0064"
down_revision: str | Sequence[str] | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enum-ish columns have ONE source of truth (lesson «An enum-ish column has ONE
# source of truth») — these mirror `applications.models.PRINTOUT_KINDS` and
# `app.core.schemas.LOCALES` as literals, since a migration is frozen history
# and does not import the app.
KINDS_SQL = "'letter', 'rejection_notice'"
LOCALES_SQL = "'uz_cyrl', 'uz_latn', 'ru', 'kaa', 'en'"

# `0005_admin_seeds.py::CLASSIFIERS`, the `rejection_reasons` row's fixed id —
# copied from `migrations/versions/0025_review_seeds.py`.
REJECTION_REASONS_CLASSIFIER_ID = "0198f100-0003-7000-8000-000000000001"
EFFECTIVE = "2026-09-24"

# (id, code, uz_latn, uz_cyrl, ru, kaa, en, legal_basis default) — the blank's
# own eight rejection codes (ruling R4), R08 the catch-all.
R_ITEMS = [
    (
        "0198f160-0016-7000-8000-000000000001",
        "R01",
        "Maʼlumot toʻliq emas yoki oʻzaro mos emas",
        "Маълумот тўлиқ эмас ёки ўзаро мос эмас",
        "Сведения неполны или противоречивы",
        "Maǵlıwmat tolıq emes yamasa óz ara sáykes emes",
        "Information incomplete or inconsistent",
        "",
    ),
    (
        "0198f160-0016-7000-8000-000000000002",
        "R02",
        "Soʻralgan foydalanish turi huquqiy roʻyxatga mos emas",
        "Сўралган фойдаланиш тури ҳуқуқий рўйхатга мос эмас",
        "Запрошенный вид пользования не предусмотрен законодательством",
        "Soralǵan paydalanıw túri huqıqıy dizimge sáykes emes",
        "Requested use is not on the legal list",
        "VMQ 278 (30.09.2015), 1-ilova",
    ),
    (
        "0198f160-0016-7000-8000-000000000003",
        "R03",
        "Uchastkada huquqiy yoki mavsumiy cheklov mavjud",
        "Участкада ҳуқуқий ёки мавсумий чеклов мавжуд",
        "На участке действует правовое или сезонное ограничение",
        "Ushastkada huqıqıy yamasa máwsimlik sheklew bar",
        "Legal or seasonal restriction on the plot",
        "VMQ 506 (22.11.1999)",
    ),
    (
        "0198f160-0016-7000-8000-000000000004",
        "R04",
        "Yaylov normasi yoki almashinish rejasiga muvofiq emas",
        "Яйлов нормаси ёки алмашиниш режасига мувофиқ эмас",
        "Не соответствует норме пастбища или плану ротации",
        "Jaylaw normasına yamasa almasıw rejesine sáykes emes",
        "Exceeds the pasture norm or rotation plan",
        "VMQ 689 (19.08.2019), 1-ilova",
    ),
    (
        "0198f160-0016-7000-8000-000000000005",
        "R05",
        "Hudud yoki koordinatada ustma-ust tushish aniqlandi",
        "Ҳудуд ёки координатада устма-уст тушиш аниқланди",
        "Выявлено наложение участков или координат",
        "Aymaqta yamasa koordinatada ústpe-úst túsiw anıqlandı",
        "Overlap in area or coordinates found",
        "",
    ),
    (
        "0198f160-0016-7000-8000-000000000006",
        "R06",
        "Dala oʻrganishida salbiy xulosa berildi",
        "Дала ўрганишида салбий хулоса берилди",
        "Отрицательное заключение полевого обследования",
        "Dala úyreniwinde unamsız juwmaq berildi",
        "Negative field inspection finding",
        "",
    ),
    (
        "0198f160-0016-7000-8000-000000000007",
        "R07",
        "Toʻlov yoki hisob-kitob sharti bajarilmagan",
        "Тўлов ёки ҳисоб-китоб шарти бажарилмаган",
        "Не выполнено условие оплаты или расчёта",
        "Tólem yamasa esap-kitap shárti orınlanbaǵan",
        "Payment or settlement condition not met",
        "VMQ 278 (30.09.2015), 1-ilova",
    ),
    (
        "0198f160-0016-7000-8000-000000000008",
        "R08",
        "Boshqa sabab",
        "Бошқа сабаб",
        "Иная причина",
        "Basqa sebep",
        "Other reason",
        "",
    ),
]
ARCHIVED_RJ = tuple(f"RJ-{i:02d}" for i in range(3, 13))  # RJ-03 ... RJ-12


def upgrade() -> None:
    """Upgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "application_printouts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=True),
        sa.Column("number", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_application_printouts_application_id_applications"),
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["media_files.id"],
            name=op.f("fk_application_printouts_file_id_media_files"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_printouts")),
        sa.UniqueConstraint("number", name=op.f("uq_application_printouts_number")),
        sa.UniqueConstraint("submission_id", name=op.f("uq_application_printouts_submission_id")),
    )
    op.create_index(
        op.f("ix_application_printouts_application_id"),
        "application_printouts",
        ["application_id"],
        unique=False,
    )
    op.create_table(
        "application_rejection_grounds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("reason_item_id", sa.Uuid(), nullable=False),
        sa.Column("fact", sa.Text(), nullable=False),
        sa.Column("legal_document", sa.Text(), nullable=False),
        sa.Column("legal_clause", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("remedy", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "position >= 1", name=op.f("ck_application_rejection_grounds_position_positive")
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_application_rejection_grounds_application_id_applications"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_application_rejection_grounds_created_by_users"),
        ),
        sa.ForeignKeyConstraint(
            ["reason_item_id"],
            ["classifier_items.id"],
            name=op.f("fk_application_rejection_grounds_reason_item_id_classifier_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_rejection_grounds")),
        sa.UniqueConstraint(
            "application_id",
            "position",
            name=op.f("uq_application_rejection_grounds_application_id_position"),
        ),
    )
    op.create_index(
        op.f("ix_application_rejection_grounds_application_id"),
        "application_rejection_grounds",
        ["application_id"],
        unique=False,
    )
    # ### end Alembic commands ###

    # --- Hand-written: checks autogenerate cannot write ------------------------
    op.create_check_constraint("kind_valid", "application_printouts", f"kind IN ({KINDS_SQL})")
    op.create_check_constraint(
        "language_valid", "application_printouts", f"language IN ({LOCALES_SQL})"
    )
    op.create_check_constraint(
        "letter_has_submission",
        "application_printouts",
        "(kind = 'letter') = (submission_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "notice_has_number",
        "application_printouts",
        "(kind = 'rejection_notice') = (number IS NOT NULL)",
    )
    op.create_check_constraint(
        "file_and_hash_together",
        "application_printouts",
        "(file_id IS NULL) = (sha256 IS NULL)",
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_application_printouts_one_notice "
        "ON application_printouts (application_id) WHERE kind = 'rejection_notice'"
    )

    # --- Hand-written: append-only trigger on application_rejection_grounds ----
    op.execute(
        """
        CREATE FUNCTION application_rejection_grounds_forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'application_rejection_grounds is append-only';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER application_rejection_grounds_append_only "
        "BEFORE UPDATE OR DELETE ON application_rejection_grounds "
        "FOR EACH ROW EXECUTE FUNCTION application_rejection_grounds_forbid_mutation()"
    )
    op.execute(
        "CREATE TRIGGER application_rejection_grounds_no_truncate "
        "BEFORE TRUNCATE ON application_rejection_grounds "
        "FOR EACH STATEMENT EXECUTE FUNCTION application_rejection_grounds_forbid_mutation()"
    )

    # --- Hand-written: freeze trigger on application_printouts ------------------
    op.execute(
        """
        CREATE FUNCTION application_printouts_freeze() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP <> 'UPDATE' THEN
                RAISE EXCEPTION 'application_printouts rows are never deleted';
            END IF;
            IF OLD.file_id IS NOT NULL
               OR NEW.id IS DISTINCT FROM OLD.id
               OR NEW.application_id IS DISTINCT FROM OLD.application_id
               OR NEW.kind IS DISTINCT FROM OLD.kind
               OR NEW.submission_id IS DISTINCT FROM OLD.submission_id
               OR NEW.number IS DISTINCT FROM OLD.number
               OR NEW.language IS DISTINCT FROM OLD.language
               OR NEW.snapshot IS DISTINCT FROM OLD.snapshot
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION
                    'an application printout is frozen: only its first render may be stored';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER application_printouts_frozen "
        "BEFORE UPDATE OR DELETE ON application_printouts "
        "FOR EACH ROW EXECUTE FUNCTION application_printouts_freeze()"
    )
    op.execute(
        "CREATE TRIGGER application_printouts_no_truncate "
        "BEFORE TRUNCATE ON application_printouts "
        "FOR EACH STATEMENT EXECUTE FUNCTION application_printouts_freeze()"
    )

    # --- Hand-written: R01..R08 seed, RJ-03..RJ-12 archived, RJ-15 return-only --
    conn = op.get_bind()
    for index, (item_id, code, latn, cyrl, ru, kaa, en, basis) in enumerate(R_ITEMS, 1):
        conn.execute(
            sa.text(
                "INSERT INTO classifier_items "
                "(id, classifier_id, code, name, props, valid_from, sort_order, status) VALUES "
                "(CAST(:id AS uuid), CAST(:classifier_id AS uuid), :code, "
                "jsonb_build_object('uz_latn', :latn, 'uz_cyrl', :cyrl, 'ru', :ru, "
                "'kaa', :kaa, 'en', :en), "
                "jsonb_build_object('kind', 'reject', 'legal_basis', :basis), "
                "CAST(:effective AS date), :sort, 'active')"
            ).bindparams(
                id=item_id,
                classifier_id=REJECTION_REASONS_CLASSIFIER_ID,
                code=code,
                latn=latn,
                cyrl=cyrl,
                ru=ru,
                kaa=kaa,
                en=en,
                basis=basis,
                effective=EFFECTIVE,
                sort=200 + index * 10,
            )
        )
    archived = conn.execute(
        sa.text(
            "UPDATE classifier_items SET status = 'archived', valid_to = CAST(:effective AS date) "
            "WHERE classifier_id = CAST(:classifier_id AS uuid) AND code IN :codes "
            "AND status = 'active'"
        ).bindparams(
            sa.bindparam("codes", value=list(ARCHIVED_RJ), expanding=True),
            effective=EFFECTIVE,
            classifier_id=REJECTION_REASONS_CLASSIFIER_ID,
        )
    ).rowcount
    if archived != len(ARCHIVED_RJ):
        raise RuntimeError(
            f"expected {len(ARCHIVED_RJ)} active RJ-03...RJ-12 rows, archived {archived}"
        )
    updated = conn.execute(
        sa.text(
            "UPDATE classifier_items SET props = jsonb_set(props, '{kind}', '\"return\"') "
            "WHERE classifier_id = CAST(:classifier_id AS uuid) AND code = 'RJ-15' "
            "AND status = 'active'"
        ).bindparams(classifier_id=REJECTION_REASONS_CLASSIFIER_ID)
    ).rowcount
    if updated != 1:
        raise RuntimeError(f"expected exactly one active RJ-15 row, updated {updated}")


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE classifier_items SET props = jsonb_set(props, '{kind}', '\"both\"') "
            "WHERE classifier_id = CAST(:classifier_id AS uuid) AND code = 'RJ-15'"
        ).bindparams(classifier_id=REJECTION_REASONS_CLASSIFIER_ID)
    )
    conn.execute(
        sa.text(
            "UPDATE classifier_items SET status = 'active', valid_to = NULL "
            "WHERE classifier_id = CAST(:classifier_id AS uuid) AND code IN :codes"
        ).bindparams(
            sa.bindparam("codes", value=list(ARCHIVED_RJ), expanding=True),
            classifier_id=REJECTION_REASONS_CLASSIFIER_ID,
        )
    )

    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(
        op.f("ix_application_rejection_grounds_application_id"),
        table_name="application_rejection_grounds",
    )
    op.drop_table("application_rejection_grounds")
    op.execute("DROP FUNCTION application_rejection_grounds_forbid_mutation()")
    op.drop_index(
        op.f("ix_application_printouts_application_id"), table_name="application_printouts"
    )
    op.drop_table("application_printouts")
    op.execute("DROP FUNCTION application_printouts_freeze()")
    # ### end Alembic commands ###

    # `application_status_history.reason_item_id` and `applications.
    # rejection_reason_item_id` may already reference an R item by the time
    # this runs (lesson «A downgrade must delete whatever its upgrade made
    # possible — and an append-only referrer blocks even the nulling UPDATE»)
    # — the history table's own append-only trigger blocks a bare UPDATE, so
    # it is disabled for the one statement that nulls the FK.
    ids = [row[0] for row in R_ITEMS]
    op.execute("ALTER TABLE application_status_history DISABLE TRIGGER USER")
    conn.execute(
        sa.text(
            "UPDATE application_status_history SET reason_item_id = NULL "
            "WHERE reason_item_id IN (SELECT CAST(x AS uuid) FROM unnest(CAST(:ids AS text[])) x)"
        ).bindparams(ids=ids)
    )
    op.execute("ALTER TABLE application_status_history ENABLE TRIGGER USER")
    conn.execute(
        sa.text(
            "UPDATE applications SET rejection_reason_item_id = NULL "
            "WHERE rejection_reason_item_id IN "
            "(SELECT CAST(x AS uuid) FROM unnest(CAST(:ids AS text[])) x)"
        ).bindparams(ids=ids)
    )
    conn.execute(
        sa.text(
            "DELETE FROM classifier_items "
            "WHERE id IN (SELECT CAST(x AS uuid) FROM unnest(CAST(:ids AS text[])) x)"
        ).bindparams(ids=ids)
    )
