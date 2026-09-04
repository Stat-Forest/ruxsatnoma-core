"""review_seeds

Stage 3.9b's ONE migration (plan `03.9-3.11-parallel-run.md`, ruling 1: one
revision per stage — task 2 owns it as the first task that needs it). Three
independent things, none of them a new table:

1. **Notification templates** for every event this stage's tasks notify on
   (ruling 15): `application.sla_approaching` (task 2, this stage's own),
   `application.returned` (task 3), `application.info_requested` (task 4)
   and `application.recalculated` (task 5) — `inapp`+`sms`, mirroring
   `0009_notifications.py`'s own shape for `application.submitted`/
   `.approved`/`.rejected`. Only `sla_approaching` is registered in
   `applications.events.NOTIFIED_EVENT_CODES` by THIS commit — the
   package-wide guard test (`test_every_event_code_this_package_passes_to_
   notify_is_registered`) asserts the registry matches actual `notify()`
   call sites exactly, and tasks 3/4/5 have not landed theirs yet. Seeding
   the template now just means those tasks need no migration of their own.

2. **RJ-15's `kind`** (ruling 3): `0005_admin_seeds.py` seeded it
   `"reject"`; `tz/10` §8.2 uses it for BOTH a rejection and a return
   («Бошқа (изоҳ мажбурий)» is the catch-all on either path), so it is
   corrected to `"both"` here — a targeted UPDATE, not a re-seed, since
   `0005`'s items carry random ids and its own downgrade still owns them.

3. **Three columns on `application_checks`** for task 7's maker-checker
   (ruling 15, ANSWERED а): `created_by` (NOT NULL), `confirmed_by` (NULL),
   `confirmed_at` (NULL). Task 7 creates no migration of its own — these
   columns are this one's, built here so they exist before it runs.
   `created_by` cannot be added NOT NULL with no way to fill it: this
   database may already hold rows `checks.run_all` wrote before this
   migration ran, so every existing row is backfilled from its own
   application's `submitted_by_user_id` (there is no earlier real actor to
   recover) before the column is closed to NULL — `checks.run_all` itself
   is corrected, in the same branch, to always pass one going forward.

Revision ID: 0025
Revises: 0022
Create Date: 2026-09-05 00:00:00.000000
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `0005_admin_seeds.py::CLASSIFIERS`, the `rejection_reasons` row's fixed id.
REJECTION_REASONS_CLASSIFIER_ID = "0198f100-0003-7000-8000-000000000001"

_BODIES: dict[str, dict[str, str]] = {
    "application.sla_approaching": {
        "uz_cyrl": "Ариза {application_number} бўйича кўриб чиқиш муддати"
        " {deadline} санасида тугайди.",
        "ru": "Срок рассмотрения заявки {application_number} истекает {deadline}.",
    },
    # Bodies below name only `application_number` on purpose (никакой другой
    # placeholder): tasks 3/4/5, not yet written, choose their own extra
    # params, and `notify()`'s `render()` leaves an unresolved `{placeholder}`
    # in place rather than raising, so guessing a wrong name here would cost
    # nothing at runtime — but a versioned template is supersede-by-archive,
    # never edited in place (CLAUDE.md), so guessing a RICHER body now would
    # cost those tasks a second migration to correct it. Keeping the wording
    # minimal keeps that door open at zero cost.
    "application.returned": {
        "uz_cyrl": "Ариза {application_number} тузатиш учун қайтарилди.",
        "ru": "Заявка {application_number} возвращена на доработку.",
    },
    "application.info_requested": {
        "uz_cyrl": "Ариза {application_number} бўйича қўшимча маълумот сўралди.",
        "ru": "По заявке {application_number} запрошена дополнительная информация.",
    },
    "application.recalculated": {
        "uz_cyrl": "Ариза {application_number} бўйича тўлов миқдори қайта ҳисобланди.",
        "ru": "Сумма по заявке {application_number} пересчитана.",
    },
}

SEED_TEMPLATES: list[tuple[str, str, dict[str, str]]] = [
    (event_code, channel, body)
    for event_code, body in _BODIES.items()
    for channel in ("inapp", "sms")
]

_SEEDED_EVENT_CODES = tuple(_BODIES)


def upgrade() -> None:
    # --- 1. Notification templates -------------------------------------
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
                # Deliberately uuid4(), not app.db.uuid7 — migrations must not
                # depend on app code that could move/rename later (0009/0018/
                # 0022's own call).
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

    # --- 2. RJ-15's kind: "reject" -> "both" -----------------------------
    conn = op.get_bind()
    updated = conn.execute(
        sa.text(
            "UPDATE classifier_items SET props = jsonb_set(props, '{kind}', '\"both\"') "
            "WHERE classifier_id = CAST(:classifier_id AS uuid) AND code = 'RJ-15' "
            "AND status = 'active'"
        ).bindparams(classifier_id=REJECTION_REASONS_CLASSIFIER_ID)
    ).rowcount
    if updated != 1:
        raise RuntimeError(
            f"expected exactly one active RJ-15 row under classifier "
            f"{REJECTION_REASONS_CLASSIFIER_ID!r}, updated {updated} — "
            "0005_admin_seeds.py seeds it, so this database did not run the "
            "chain this revision depends on"
        )

    # --- 3. application_checks' three maker-checker columns --------------
    op.add_column("application_checks", sa.Column("created_by", sa.Uuid(), nullable=True))
    op.add_column("application_checks", sa.Column("confirmed_by", sa.Uuid(), nullable=True))
    op.add_column(
        "application_checks", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_application_checks_created_by_users"),
        "application_checks",
        "users",
        ["created_by"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_application_checks_confirmed_by_users"),
        "application_checks",
        "users",
        ["confirmed_by"],
        ["id"],
    )
    # Backfill every pre-existing row to its own application's owner — there
    # is no earlier real actor to recover (lesson: a NOT NULL column added to
    # a table that may already hold rows needs a backfill before it can close).
    op.execute(
        "UPDATE application_checks ac SET created_by = a.submitted_by_user_id "
        "FROM applications a WHERE a.id = ac.application_id AND ac.created_by IS NULL"
    )
    op.alter_column("application_checks", "created_by", nullable=False)


def downgrade() -> None:
    # --- 3, reversed first: application_checks' three columns ------------
    op.drop_constraint(
        op.f("fk_application_checks_confirmed_by_users"), "application_checks", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_application_checks_created_by_users"), "application_checks", type_="foreignkey"
    )
    op.drop_column("application_checks", "confirmed_at")
    op.drop_column("application_checks", "confirmed_by")
    op.drop_column("application_checks", "created_by")

    # --- 2, reversed: RJ-15's kind back to "reject" -----------------------
    op.execute(
        sa.text(
            "UPDATE classifier_items SET props = jsonb_set(props, '{kind}', '\"reject\"') "
            "WHERE classifier_id = CAST(:classifier_id AS uuid) AND code = 'RJ-15' "
            "AND status = 'active'"
        ).bindparams(classifier_id=REJECTION_REASONS_CLASSIFIER_ID)
    )

    # --- 1, reversed: notifications before their templates (lesson: a
    # downgrade must delete whatever its upgrade made possible — a sent
    # notification's `template_id` FK would otherwise break the round-trip
    # the moment any of these four codes is actually sent, exactly as
    # `0010`'s own template seed once did) --------------------------------
    _codes = ", ".join(f"'{code}'" for code in _SEEDED_EVENT_CODES)
    op.execute(f"DELETE FROM notifications WHERE event_code IN ({_codes})")
    op.execute(f"DELETE FROM notification_templates WHERE event_code IN ({_codes})")
