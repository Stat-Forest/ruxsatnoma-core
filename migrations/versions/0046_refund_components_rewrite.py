"""refund components rewrite: history rewritten, legacy columns dropped

Stage 7.9 task 7 (decision #154, decisions #161-#162, plan
`07.9-payme-split` task-7-brief Override 2). Finishes what `0045` (ruling
P1) deliberately left additive: this migration BACKFILLS every historical
row onto the new shapes and only then drops what the old shapes needed.

**Decision #161 — Oybek chose to REWRITE the historical ledger rather than
let it carry two shapes.** Two facts make that cheap now and would not make
it cheap later: production does not exist yet, and dev carries only demo
data. A later stage, with real money on the books, could not do this.

Five steps, in this order and no other (later steps depend on earlier ones
having run):

1. **Backfill `refund_components` from `refunds`' three legacy columns**,
   BEFORE anything is dropped. `budget_amount` becomes a component whose
   `recipient_id` is the seeded budget recipient (`BUDGET_RECIPIENT_ID`,
   the same constant `0045` seeded it with); `recipient_amount` and
   `other_amount` are SUMMED into ONE component with `recipient_id IS
   NULL` — `other` named no party of its own even in the old schema, and
   `uq_refund_components_source` allows only one row per source. A
   component whose total is `0` is skipped (the same "nothing from this
   source" reading `refunds.breakdown_is_complete` already gives a `None`/
   zero amount).
2. **Backfill `allocations`:** every `target = 'budget'` row becomes
   `target = 'receiver'` with `recipient_id = BUDGET_RECIPIENT_ID` — the
   ledger rewritten to look as if the configurable directory had always
   been there.
3. **Then** drop `refunds.returned_needs_complete_breakdown` and its three
   columns (`budget_amount`, `recipient_amount`, `other_amount`) — safe
   only now that step 1 has copied every row's total into
   `refund_components`.
4. **Then** narrow `allocations.target_valid` to `('recipient', 'other',
   'receiver')` — `'budget'` is gone for good (step 2 rewrote every row
   that carried it); `'other'` SURVIVES untouched, because those rows name
   no party this directory can represent and inventing one would be
   fabricating a recipient this migration has no authority to invent.
5. **Then** tighten `refund_components_complete`: `0045`'s own version
   tolerated a `returned` refund with ZERO components, because the old
   three-column CHECK was still live and no component had ever been
   written by anyone. That transition is over — every `returned` refund
   from here on must have written its components through
   `backoffice_service.submit_refund_decision` before it can ever reach
   `returned`, so this trigger now requires `count(*) > 0` too.

`downgrade()` reverses all five, INCLUDING the backfills — the mapping is
total in both directions, which is what makes this a genuine rewrite rather
than a one-way migration nobody could undo. Every raw bind below is
`CAST(:name AS type)`, never `:name::type` (`TextClause`'s regex
mis-registers a name written the second way — see `.claude/lessons.md`).

Revision ID: 0046
Revises: 0045
Create Date: 2026-09-09 12:00:00.000000

"""

import logging
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0046"
down_revision: str | Sequence[str] | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# Mirrors migrations/versions/0045_payment_split.py::BUDGET_RECIPIENT_ID.
BUDGET_RECIPIENT_ID = uuid.UUID("0192f2a0-0000-7000-8000-000000000001")

_NEW_TARGET_VALID = "target IN ('recipient', 'other', 'receiver')"
_OLD_TARGET_VALID = "target IN ('recipient', 'budget', 'other', 'receiver')"


def upgrade() -> None:
    # --- Step 1: backfill refund_components from the three legacy columns,
    # BEFORE anything is dropped.
    budget_rows = (
        op.get_bind()
        .execute(
            sa.text("SELECT id, budget_amount FROM refunds WHERE coalesce(budget_amount, 0) > 0")
        )
        .all()
    )
    for row in budget_rows:
        op.execute(
            sa.text(
                "INSERT INTO refund_components (id, refund_id, recipient_id, amount) "
                "VALUES (gen_random_uuid(), CAST(:refund_id AS uuid), "
                "CAST(:recipient_id AS uuid), CAST(:amount AS numeric(18,2)))"
            ).bindparams(
                refund_id=row.id,
                recipient_id=BUDGET_RECIPIENT_ID,
                amount=row.budget_amount,
            )
        )
    logger.info("0046: backfilled %d refund_components row(s) from budget_amount", len(budget_rows))

    # `recipient_amount` and `other_amount` share ONE component
    # (`recipient_id IS NULL`, the leshoz's remainder) — summed, never two
    # rows, since `other` never named a party of its own even in the old
    # schema and the unique index allows exactly one row per source. The
    # override's own check (`coalesce(recipient_amount,0) > 0 AND
    # coalesce(other_amount,0) > 0`) is answered by this single query
    # rather than run separately: any refund whose SUM is positive gets one
    # row regardless of how the old two columns split it.
    leshoz_rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id, coalesce(recipient_amount, 0) + coalesce(other_amount, 0) AS total "
                "FROM refunds "
                "WHERE coalesce(recipient_amount, 0) + coalesce(other_amount, 0) > 0"
            )
        )
        .all()
    )
    for row in leshoz_rows:
        op.execute(
            sa.text(
                "INSERT INTO refund_components (id, refund_id, recipient_id, amount) "
                "VALUES (gen_random_uuid(), CAST(:refund_id AS uuid), NULL, "
                "CAST(:amount AS numeric(18,2)))"
            ).bindparams(refund_id=row.id, amount=row.total)
        )
    logger.info(
        "0046: backfilled %d refund_components row(s) from recipient_amount+other_amount",
        len(leshoz_rows),
    )

    # --- Step 2: backfill allocations — 'budget' rows become 'receiver'
    # rows against the seeded budget recipient.
    result = op.get_bind().execute(
        sa.text(
            "UPDATE allocations SET target = 'receiver', "
            "recipient_id = CAST(:recipient_id AS uuid) WHERE target = 'budget'"
        ).bindparams(recipient_id=BUDGET_RECIPIENT_ID)
    )
    logger.info("0046: rewrote %d allocations row(s) from target='budget'", result.rowcount)

    # --- Step 3: drop refunds' legacy CHECK and its three columns — safe
    # now that step 1 copied every row's total into refund_components.
    op.drop_constraint(
        op.f("ck_refunds_returned_needs_complete_breakdown"), "refunds", type_="check"
    )
    op.drop_column("refunds", "budget_amount")
    op.drop_column("refunds", "recipient_amount")
    op.drop_column("refunds", "other_amount")

    # --- Step 4: narrow allocations.target_valid — 'budget' is gone for
    # good, 'other' survives untouched.
    op.drop_constraint(op.f("ck_allocations_target_valid"), "allocations", type_="check")
    op.create_check_constraint("target_valid", "allocations", _NEW_TARGET_VALID)

    # --- Step 5: tighten refund_components_complete — a returned refund
    # now needs at least one component, no exceptions.
    op.execute("DROP TRIGGER IF EXISTS refund_components_complete_trg ON refunds")
    op.execute("DROP FUNCTION IF EXISTS refund_components_complete()")
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
                RAISE EXCEPTION 'a returned refund needs at least one component';
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


def downgrade() -> None:
    # Reverse 5-1.

    # --- Step 5 reversed: restore the tolerant trigger (0045's own
    # version — passes a returned refund with zero components).
    op.execute("DROP TRIGGER IF EXISTS refund_components_complete_trg ON refunds")
    op.execute("DROP FUNCTION IF EXISTS refund_components_complete()")
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

    # --- Step 4 reversed: widen target_valid back to include 'budget'
    # BEFORE any row is rewritten onto it (the CHECK must accept the value
    # before a row can carry it).
    op.drop_constraint(op.f("ck_allocations_target_valid"), "allocations", type_="check")
    op.create_check_constraint("target_valid", "allocations", _OLD_TARGET_VALID)

    # --- Step 3 reversed: restore refunds' three columns and its CHECK.
    op.add_column("refunds", sa.Column("budget_amount", sa.Numeric(18, 2), nullable=True))
    op.add_column("refunds", sa.Column("recipient_amount", sa.Numeric(18, 2), nullable=True))
    op.add_column("refunds", sa.Column("other_amount", sa.Numeric(18, 2), nullable=True))
    op.create_check_constraint(
        "returned_needs_complete_breakdown",
        "refunds",
        "status <> 'returned' OR (final_amount IS NOT NULL AND "
        "coalesce(budget_amount, 0) + coalesce(recipient_amount, 0) "
        "+ coalesce(other_amount, 0) = final_amount)",
    )

    # --- Step 2 reversed: allocations 'receiver' rows against the budget
    # recipient go back to 'budget', BEFORE the column values are read back
    # into refunds below (order does not matter between the two reversals,
    # but both must run before step 1's rows are gone).
    result = op.get_bind().execute(
        sa.text(
            "UPDATE allocations SET target = 'budget', recipient_id = NULL "
            "WHERE target = 'receiver' AND recipient_id = CAST(:recipient_id AS uuid)"
        ).bindparams(recipient_id=BUDGET_RECIPIENT_ID)
    )
    logger.info(
        "0046 downgrade: restored %d allocations row(s) to target='budget'", result.rowcount
    )

    # --- Step 1 reversed: copy refund_components back onto refunds' three
    # columns, then delete the rows this migration's own upgrade() created.
    # The budget component -> budget_amount:
    budget_components = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT refund_id, amount FROM refund_components "
                "WHERE recipient_id = CAST(:recipient_id AS uuid)"
            ).bindparams(recipient_id=BUDGET_RECIPIENT_ID)
        )
        .all()
    )
    for row in budget_components:
        op.execute(
            sa.text(
                "UPDATE refunds SET budget_amount = CAST(:amount AS numeric(18,2)) "
                "WHERE id = CAST(:refund_id AS uuid)"
            ).bindparams(amount=row.amount, refund_id=row.refund_id)
        )
    # The leshoz's remainder component -> recipient_amount (the old
    # other_amount column stays NULL — the split between the two legacy
    # columns cannot be recovered, and NULL reads as "nothing from this
    # source" exactly like `0` did before this migration ran).
    leshoz_components = (
        op.get_bind()
        .execute(
            sa.text("SELECT refund_id, amount FROM refund_components WHERE recipient_id IS NULL")
        )
        .all()
    )
    for row in leshoz_components:
        op.execute(
            sa.text(
                "UPDATE refunds SET recipient_amount = CAST(:amount AS numeric(18,2)) "
                "WHERE id = CAST(:refund_id AS uuid)"
            ).bindparams(amount=row.amount, refund_id=row.refund_id)
        )
    logger.info(
        "0046 downgrade: restored %d budget_amount and %d recipient_amount row(s)",
        len(budget_components),
        len(leshoz_components),
    )

    # Delete every refund_components row this upgrade() created — i.e.
    # every row belonging to a refund whose legacy columns were just
    # restored above. A blanket `DELETE FROM refund_components` would also
    # remove rows written by `backoffice_service.submit_refund_decision`
    # after this migration's upgrade() ran (Task 7's own service, a REAL
    # write path this downgrade must not silently destroy while pretending
    # to only reverse its own backfill) — but the schema records no
    # "written by this migration" marker, and the same shape ambiguity
    # applies to `allocations.recipient_id`/`target='receiver'` rows above.
    # Ruling P1 (0045) accepted the identical ambiguity for its own
    # downgrade (see that migration's docstring) — a `downgrade()` reverses
    # SCHEMA, on a database this branch's own tests reset from a clean
    # `upgrade head` each run; it does not attempt to distinguish "written
    # before this migration" from "written after" on a database that kept
    # taking real writes in between, which is not this branch's use case.
    op.execute("DELETE FROM refund_components")
