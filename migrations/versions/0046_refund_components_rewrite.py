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

`downgrade()` reverses all five, INCLUDING the backfills — but the mapping
is total in only ONE direction (whole-branch review Minor 9: this used to
claim "both", which the `downgrade()` code below already contradicted
itself). Forward (`upgrade()`), the fold is exact: `recipient_amount` and
`other_amount` sum into one `recipient_id IS NULL` component, and that sum
is all `refund_components` ever needs, because nothing downstream reads
`other_amount` on its own again. Backward, that sum cannot un-split back
into the two numbers it came from — `other` never named a party of its own
even in the OLD schema, so there is no second value to recover, only one
to invent. The books still balance either way (`recipient_amount` restored
is the correct TOTAL, and the trigger's own sum check passes), the
provenance of that total does not (`other_amount` comes back `NULL` on
every row, never a guess) — accepted because a component naming no party
was already unrecoverable information, not information this migration
loses on the way down. Every raw bind below is `CAST(:name AS type)`,
never `:name::type` (`TextClause`'s regex mis-registers a name written the
second way — see `.claude/lessons.md`).

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
    # NOT VALID: at this exact instant the three columns are all NULL for
    # EVERY row (step 1 reversed, below, is what fills them back in) — a
    # validating ADD CONSTRAINT checks every existing row immediately, so
    # any `returned` refund with a nonzero `final_amount` would hit
    # coalesce(NULL,0)*3 = 0 <> final_amount and crash the whole downgrade
    # before a single value came back (review round 1, CRITICAL 1). NOT
    # VALID defers that check to the explicit VALIDATE CONSTRAINT below,
    # run only after step 1 reversed has restored real values — the same
    # two-step this codebase already uses in
    # migrations/versions/0003_auth.py:221 (an FK there, a CHECK here, same
    # reason: don't validate over existing data before that data is right).
    op.create_check_constraint(
        "returned_needs_complete_breakdown",
        "refunds",
        "status <> 'returned' OR (final_amount IS NOT NULL AND "
        "coalesce(budget_amount, 0) + coalesce(recipient_amount, 0) "
        "+ coalesce(other_amount, 0) = final_amount)",
        postgresql_not_valid=True,
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
    #
    # Both columns for the SAME refund are set in ONE UPDATE, never two
    # sequential single-column ones (the original shape of this section,
    # and a SECOND instance of review round 1's CRITICAL 1): the CHECK
    # re-added above is NOT VALID only for rows that already existed when
    # it was created — Postgres still enforces it on every write from that
    # point on, valid or not. Two separate UPDATEs would carry a row
    # through an intermediate state with `budget_amount` set and
    # `recipient_amount` still NULL, which fails the sum check exactly
    # like the constraint-creation bug did, just one statement later. A
    # single UPDATE with two correlated subqueries computes BOTH new
    # values before the row is written, so the CHECK only ever sees each
    # row's final, complete shape — proven against a scratch table before
    # this line shipped (see this task's own fix report).
    #
    # The old `other_amount` column stays NULL for every row — the split
    # between the two legacy columns cannot be recovered once folded into
    # one leshoz component, and NULL reads as "nothing from this source"
    # exactly like `0` did before this migration ran.
    budget_count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM refund_components "
                "WHERE recipient_id = CAST(:recipient_id AS uuid)"
            ).bindparams(recipient_id=BUDGET_RECIPIENT_ID)
        )
        .scalar_one()
    )
    leshoz_count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM refund_components WHERE recipient_id IS NULL"))
        .scalar_one()
    )
    # SUM(), not a bare `amount` column read: `uq_refund_components_source`
    # does NOT stop two `recipient_id IS NULL` rows on the SAME refund
    # (`RefundComponent`'s own docstring — Postgres treats `NULL <> NULL`
    # under a plain UNIQUE constraint; only the SERVICE layer refuses that
    # duplicate, per Override 1). A bare `(SELECT amount FROM ... )`
    # correlated subquery raises `CardinalityViolationError` the instant
    # such a pair exists for one refund — hit for real against this
    # worktree's own accumulated test data while proving this fix (a
    # leftover pair from an earlier manual guard-removal check, `amount`
    # 300000.00 each). An aggregate always collapses to exactly one row
    # (NULL when nothing matches), so this is correct for the normal
    # single-row case and merely SUMS instead of crashing for the
    # anomalous one — consistent with what `recipient_amount` always meant
    # under the old schema: everything that went to the leshoz's own
    # account, not "the single row that happened to be there."
    # Minor 7 (whole-branch review), same reasoning as BLOCKER 1: the SUM()
    # below silently ACCOMMODATES two `recipient_id IS NULL` rows on one
    # refund (Postgres' `NULL <> NULL` means `uq_refund_components_source`
    # cannot stop it — only `submit_refund_decision`'s own Override 1
    # check does, and only for writes after this migration's `upgrade()`).
    # A row pair like that reaching this downgrade is an anomaly, not a
    # normal case the SUM should absorb quietly — logged BEFORE summing so
    # it is diagnosable rather than indistinguishable from the single-row
    # case that is this loop's actual design.
    duplicate_leshoz_refunds = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM (SELECT 1 FROM refund_components "
                "WHERE recipient_id IS NULL GROUP BY refund_id HAVING count(*) > 1) dupes"
            )
        )
        .scalar_one()
    )
    if duplicate_leshoz_refunds:
        logger.warning(
            "0046 downgrade: %d refund(s) carry more than one recipient_id IS NULL "
            "refund_components row - recipient_amount below is their SUM, not one "
            "row's own amount",
            duplicate_leshoz_refunds,
        )

    op.execute(
        sa.text(
            "UPDATE refunds SET "
            "budget_amount = (SELECT sum(amount) FROM refund_components "
            "WHERE refund_id = refunds.id AND recipient_id = CAST(:recipient_id AS uuid)), "
            "recipient_amount = (SELECT sum(amount) FROM refund_components "
            "WHERE refund_id = refunds.id AND recipient_id IS NULL) "
            "WHERE id IN (SELECT refund_id FROM refund_components "
            "WHERE recipient_id = CAST(:recipient_id AS uuid) OR recipient_id IS NULL)"
        ).bindparams(recipient_id=BUDGET_RECIPIENT_ID)
    )
    logger.info(
        "0046 downgrade: restored %d budget_amount and %d recipient_amount row(s)",
        budget_count,
        leshoz_count,
    )

    # Now that every row's legacy columns carry real values, validate the
    # NOT VALID constraint created above — the deferred half of the
    # NOT VALID -> VALIDATE two-step (review round 1, CRITICAL 1). A row
    # whose breakdown genuinely does not add up (should not exist: the OLD
    # CHECK enforced this invariant on every write until this migration's
    # upgrade() dropped it) raises here, loudly, rather than silently
    # leaving an unenforced CHECK behind.
    op.execute(
        "ALTER TABLE refunds VALIDATE CONSTRAINT ck_refunds_returned_needs_complete_breakdown"
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
