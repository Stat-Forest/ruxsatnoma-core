"""payment split: recipients directory, invoice snapshot, refund components

Stage 7.9 task 1 (decisions #154-#159, plan `07.9-payme-split` task-1-brief,
ruling P1). Three new tables:

- `payment_recipients` — the directory of parties who take something off the
  top of every payment before the leshoz receives the remainder.
- `invoice_recipients` — the split FROZEN onto one invoice at issuance.
- `refund_components` — a refund's breakdown by source, replacing the old
  three-column CHECK's job with a trigger (`refund_components_complete`)
  that spans rows.

Plus `allocations.recipient_id` (nullable FK to `payment_recipients`) and
`'receiver'` joining `allocations.target_valid`'s CHECK.

**Ruling P1 — this migration is PURELY ADDITIVE.** It drops nothing and
backfills nothing: `refunds.budget_amount`/`recipient_amount`/`other_amount`
and their own CHECK (`returned_needs_complete_breakdown`) stay exactly as
they are, `'budget'` stays legal in `target_valid` alongside the new
`'receiver'`, and no existing `allocations`/`refunds` row is rewritten. A
later migration (0046), once the code that writes those legacy columns has
itself been rewritten, does the backfill and drops what this one leaves
behind.

**The `refund_components_complete` trigger tolerates the transition it is
born into.** With the legacy three-column CHECK still live and no
`refund_components` row written by anyone yet, a `returned` refund with ZERO
components must still be updatable — the old CHECK is what balances it, via
`budget_amount`. The trigger only enforces "components sum to
`final_amount`" once at least one component row exists for that refund.
Migration 0046 tightens this to `count(*) > 0` when the legacy columns go.

The budget directory row is seeded with a hard-coded id
(`BUDGET_RECIPIENT_ID`, not `gen_random_uuid()`) so dev, the test database
and any future environment mean the same row — the eventual backfill (0046)
needs to address exactly this one.

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-09 09:00:00.000000

"""

import uuid
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0045"
down_revision: str | Sequence[str] | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Stable, not random (see the module docstring) — migration 0046's own
# backfill addresses exactly this row.
BUDGET_RECIPIENT_ID = uuid.UUID("0192f2a0-0000-7000-8000-000000000001")

_OLD_TARGET_VALID = "target IN ('recipient', 'budget', 'other')"
_NEW_TARGET_VALID = "target IN ('recipient', 'budget', 'other', 'receiver')"


def upgrade() -> None:
    # 1. payment_recipients — the directory.
    op.create_table(
        "payment_recipients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payme_account_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("percent", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("fixed_amount", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
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
            "kind IN ('percent', 'fixed')", name=op.f("ck_payment_recipients_kind_valid")
        ),
        sa.CheckConstraint(
            "(kind = 'percent' AND percent IS NOT NULL AND fixed_amount IS NULL "
            "AND percent > 0 AND percent <= 100) OR "
            "(kind = 'fixed' AND fixed_amount IS NOT NULL AND percent IS NULL "
            "AND fixed_amount > 0)",
            name=op.f("ck_payment_recipients_rule_matches_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_payment_recipients_created_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_recipients")),
    )

    # 2. invoice_recipients — the split frozen at issuance.
    op.create_table(
        "invoice_recipients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=True),
        sa.Column("name", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payme_account_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("percent", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("fixed_amount", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('percent', 'fixed', 'remainder')",
            name=op.f("ck_invoice_recipients_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], name=op.f("fk_invoice_recipients_invoice_id_invoices")
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["payment_recipients.id"],
            name=op.f("fk_invoice_recipients_recipient_id_payment_recipients"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_recipients")),
        sa.UniqueConstraint("invoice_id", "position", name=op.f("uq_invoice_recipients_position")),
    )
    op.create_index(
        op.f("ix_invoice_recipients_invoice_id"), "invoice_recipients", ["invoice_id"], unique=False
    )

    # 3. refund_components — a refund's breakdown by source.
    op.create_table(
        "refund_components",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("refund_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_refund_components_amount_positive")),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["payment_recipients.id"],
            name=op.f("fk_refund_components_recipient_id_payment_recipients"),
        ),
        sa.ForeignKeyConstraint(
            ["refund_id"], ["refunds.id"], name=op.f("fk_refund_components_refund_id_refunds")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refund_components")),
        sa.UniqueConstraint("refund_id", "recipient_id", name=op.f("uq_refund_components_source")),
    )
    op.create_index(
        op.f("ix_refund_components_refund_id"), "refund_components", ["refund_id"], unique=False
    )

    # 4. allocations.recipient_id.
    op.add_column("allocations", sa.Column("recipient_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_allocations_recipient_id"), "allocations", ["recipient_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_allocations_recipient_id_payment_recipients"),
        "allocations",
        "payment_recipients",
        ["recipient_id"],
        ["id"],
    )

    # 5. allocations.target_valid: 'receiver' added, 'budget' KEPT (ruling P1).
    op.drop_constraint(op.f("ck_allocations_target_valid"), "allocations", type_="check")
    op.create_check_constraint("target_valid", "allocations", _NEW_TARGET_VALID)

    # 6. refund_components_complete trigger (ruling R4) — tolerant of zero
    # components, see the module docstring.
    op.execute(
        """
        CREATE FUNCTION refund_components_complete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            component_count integer;
            total numeric(18,2);
        BEGIN
            IF NEW.status <> 'returned' THEN
                RETURN NEW;
            END IF;
            IF NEW.final_amount IS NULL THEN
                RAISE EXCEPTION 'a returned refund needs final_amount';
            END IF;
            SELECT count(*), coalesce(sum(amount), 0) INTO component_count, total
            FROM refund_components WHERE refund_id = NEW.id;
            IF component_count = 0 THEN
                RETURN NEW;
            END IF;
            IF total <> NEW.final_amount THEN
                RAISE EXCEPTION 'refund components (%) do not sum to final_amount (%)',
                    total, NEW.final_amount;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER refund_components_complete_trg "
        "BEFORE UPDATE ON refunds "
        "FOR EACH ROW EXECUTE FUNCTION refund_components_complete()"
    )

    # 7. Seed the budget directory row (VMQ 278 default 50%).
    recipients = sa.table(
        "payment_recipients",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("name", postgresql.JSONB()),
        sa.column("kind", sa.String()),
        sa.column("percent", sa.Numeric(5, 2)),
        sa.column("active", sa.Boolean()),
        sa.column("sort_order", sa.Integer()),
        sa.column("note", sa.String()),
    )
    op.bulk_insert(
        recipients,
        [
            {
                "id": BUDGET_RECIPIENT_ID,
                "name": {
                    "uz_latn": "Davlat byudjeti",
                    "uz_cyrl": "Давлат бюджети",
                    "ru": "Государственный бюджет",
                },
                "kind": "percent",
                "percent": Decimal("50.00"),
                "active": True,
                "sort_order": 10,
                "note": "VMQ 278 default. Payme account id to be filled in by the Agency (#159).",
            }
        ],
    )


def downgrade() -> None:
    # Reverse 7-1, in that order.
    op.execute(
        sa.text("DELETE FROM payment_recipients WHERE id = CAST(:id AS uuid)").bindparams(
            id=str(BUDGET_RECIPIENT_ID)
        )
    )

    op.execute("DROP TRIGGER IF EXISTS refund_components_complete_trg ON refunds")
    op.execute("DROP FUNCTION IF EXISTS refund_components_complete()")

    op.drop_constraint(op.f("ck_allocations_target_valid"), "allocations", type_="check")
    op.create_check_constraint("target_valid", "allocations", _OLD_TARGET_VALID)

    op.drop_constraint(
        op.f("fk_allocations_recipient_id_payment_recipients"), "allocations", type_="foreignkey"
    )
    op.drop_index(op.f("ix_allocations_recipient_id"), table_name="allocations")
    op.drop_column("allocations", "recipient_id")

    op.drop_index(op.f("ix_refund_components_refund_id"), table_name="refund_components")
    op.drop_table("refund_components")

    op.drop_index(op.f("ix_invoice_recipients_invoice_id"), table_name="invoice_recipients")
    op.drop_table("invoice_recipients")

    op.drop_table("payment_recipients")
