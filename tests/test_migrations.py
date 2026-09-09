"""Прогон миграций на тестовой БД: upgrade head проходит, расширения на месте,
autogenerate не видит расхождений (страж от C1: DROP TABLE spatial_ref_sys)."""

import asyncio
import uuid
from decimal import Decimal

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from geoalchemy2 import alembic_helpers
from sqlalchemy import text

from app.config import get_settings
from app.db import Base

# Mirrors migrations/versions/0045_payment_split.py::BUDGET_RECIPIENT_ID —
# that module's name starts with a digit and cannot be imported.
_BUDGET_RECIPIENT_ID = uuid.UUID("0192f2a0-0000-7000-8000-000000000001")


def _alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.attributes["sqlalchemy_url"] = url
    return cfg


async def test_upgrade_head_installs_extensions(engine):
    url = get_settings().database_url_test
    # env.py (шаблон -t async) сам крутит event loop — из async-теста зовём в отдельном потоке
    await asyncio.to_thread(command.upgrade, _alembic_config(url), "head")
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT extname FROM pg_extension WHERE extname IN "
                "('postgis','btree_gist','pg_trgm','citext','unaccent')"
            )
        )
        assert {r[0] for r in rows} == {"postgis", "btree_gist", "pg_trgm", "citext", "unaccent"}


async def test_autogenerate_diff_empty(engine):
    # Не полагаемся на порядок тестов — прогоняем upgrade head сами (идемпотентно).
    url = get_settings().database_url_test
    await asyncio.to_thread(command.upgrade, _alembic_config(url), "head")

    async with engine.connect() as conn:

        def _diff(sync_conn):
            ctx = MigrationContext.configure(
                sync_conn,
                opts={"compare_type": True, "include_object": alembic_helpers.include_object},
            )
            return compare_metadata(ctx, Base.metadata)

        diff = await conn.run_sync(_diff)
    assert diff == []


async def test_0046_downgrade_survives_a_returned_refund_with_real_components(engine):
    """Review round 1, CRITICAL 1: `0046`'s `downgrade()` used to re-add
    `refunds`' three legacy columns as all-NULL and immediately create
    `returned_needs_complete_breakdown` — a plain CHECK, which Postgres
    validates against every EXISTING row the instant `ADD CONSTRAINT`
    runs, before the columns were ever populated. Any `status='returned'`
    row with a nonzero `final_amount` made that `ALTER TABLE` raise
    `CheckViolation` and abort the whole downgrade.

    `test_downgrade_upgrade_roundtrip` (below) cannot catch this on its
    own: it always runs on an EMPTY table (base -> head -> base -> head,
    first thing in a fresh worker database), so the CHECK it re-creates
    never has a row to validate against — the exact gap the reviewer
    flagged. This test builds one `returned` refund with a REAL,
    non-trivial breakdown (by raw SQL — the ORM's `Refund`/`RefundComponent`
    models are the HEAD shape, and this is deliberately data as it would
    exist BEFORE downgrade runs) and proves `downgrade()` both survives it
    and restores the right values, the walked-on-paper example from the
    finding itself: budget_amount=300000.00, recipient_amount=300000.00
    (the leshoz's own remainder, `recipient_id IS NULL`), final_amount
    600000.00."""
    url = get_settings().database_url_test
    cfg = _alembic_config(url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    refund_id = uuid.uuid4()
    user_id = uuid.uuid4()
    applicant_id = uuid.uuid4()
    application_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    # 14 numeric digits (`pinfl_format`), derived from the applicant's own
    # id so two runs of this test — or a run that crashed before its own
    # cleanup below ran — never collide on `uq_applicants_pinfl`.
    pinfl = str(applicant_id.int % 10**14).zfill(14)

    async with engine.begin() as conn:
        # Only what the FK chain demands, and nothing this table's own
        # invariants don't require: a role (already seeded), one user, one
        # applicant, one APPROVED application, one paid invoice, and the
        # refund itself — inserted directly as `status='returned'`.
        # `refund_components_complete` fires on UPDATE only, never INSERT
        # (test_backoffice_models.py's own note), so a bare INSERT like
        # this bypasses it on purpose — this test is about the MIGRATION's
        # downgrade, not that trigger.
        role_id = (await conn.execute(text("SELECT id FROM roles LIMIT 1"))).scalar_one()
        basis_item_id = (
            await conn.execute(
                text(
                    "SELECT ci.id FROM classifier_items ci "
                    "JOIN classifiers c ON c.id = ci.classifier_id "
                    "WHERE c.code = 'refund_reasons' AND ci.code = 'RF-03'"
                )
            )
        ).scalar_one()

        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, full_name, role_id, status, must_change_password, failed_login_count) "
                "VALUES (CAST(:id AS uuid), 'Migration test user', "
                "CAST(:role_id AS uuid), 'active', false, 0)"
            ),
            {"id": user_id, "role_id": role_id},
        )
        await conn.execute(
            text(
                "INSERT INTO applicants (id, kind, pinfl, name, owner_user_id) "
                "VALUES (CAST(:id AS uuid), 'individual', :pinfl, "
                "'Migration test applicant', CAST(:user_id AS uuid))"
            ),
            {"id": applicant_id, "pinfl": pinfl, "user_id": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO applications "
                "(id, applicant_id, submitted_by_user_id, on_behalf, channel, status, kind) "
                "VALUES (CAST(:id AS uuid), CAST(:applicant_id AS uuid), "
                "CAST(:user_id AS uuid), 'self', 'portal', 'APPROVED', 'new')"
            ),
            {"id": application_id, "applicant_id": applicant_id, "user_id": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO invoices (id, number, application_id, amount, status) "
                "VALUES (CAST(:id AS uuid), :number, CAST(:application_id AS uuid), "
                "600000.00, 'paid')"
            ),
            {
                "id": invoice_id,
                "number": f"TEST-0046-{invoice_id.hex[:12]}",
                "application_id": application_id,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO refunds "
                "(id, application_id, invoice_id, basis_item_id, final_amount, "
                "status, requested_at, due_at) "
                "VALUES (CAST(:id AS uuid), CAST(:application_id AS uuid), "
                "CAST(:invoice_id AS uuid), CAST(:basis_item_id AS uuid), "
                "600000.00, 'returned', now(), '2026-10-01')"
            ),
            {
                "id": refund_id,
                "application_id": application_id,
                "invoice_id": invoice_id,
                "basis_item_id": basis_item_id,
            },
        )
        # Two components: the budget recipient (seeded by 0045, always
        # present at head) and the leshoz's own remainder
        # (`recipient_id IS NULL`) — summing to `final_amount`, exactly the
        # shape `approve_refund` writes for a real split.
        await conn.execute(
            text(
                "INSERT INTO refund_components (id, refund_id, recipient_id, amount) "
                "VALUES (gen_random_uuid(), CAST(:refund_id AS uuid), "
                "CAST(:recipient_id AS uuid), 300000.00)"
            ),
            {"refund_id": refund_id, "recipient_id": _BUDGET_RECIPIENT_ID},
        )
        await conn.execute(
            text(
                "INSERT INTO refund_components (id, refund_id, recipient_id, amount) "
                "VALUES (gen_random_uuid(), CAST(:refund_id AS uuid), NULL, 300000.00)"
            ),
            {"refund_id": refund_id},
        )

    try:
        # THE PROOF: this must not raise. Before the fix, the CHECK
        # created inside "step 3 reversed" validated immediately against
        # this row's still-NULL columns and raised CheckViolation right
        # here, aborting the whole downgrade.
        await asyncio.to_thread(command.downgrade, cfg, "0045")

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT budget_amount, recipient_amount, other_amount, final_amount "
                        "FROM refunds WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": refund_id},
                )
            ).one()
        assert row.budget_amount == Decimal("300000.00")
        assert row.recipient_amount == Decimal("300000.00")
        assert row.other_amount is None
        assert row.final_amount == Decimal("600000.00")
    finally:
        # Whether the assertions above passed or the downgrade raised,
        # leave the schema at head: the next test in this file
        # (test_downgrade_upgrade_roundtrip) must find it there, and so
        # must every other worker/test relying on the autouse migration to
        # head having already happened. A bare upgrade to an
        # already-current head is a no-op, so this is safe even when the
        # `try` block never got past the downgrade call.
        await asyncio.to_thread(command.upgrade, cfg, "head")

        # This test's own rows would otherwise sit in the shared
        # per-worker database forever — nothing about a normal `pytest`
        # run cleans it up, and the ONLY reason `test_downgrade_upgrade_
        # roundtrip` (below) gets away with never doing this is that it
        # wipes the entire database itself, right after. Deleted in FK
        # order; `refund_components` is whatever upgrading back to head
        # just recreated from this refund's own restored legacy columns
        # (0046's own upgrade() step 1), not the rows this test inserted
        # directly — both are cleared here all the same.
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM refund_components WHERE refund_id = CAST(:id AS uuid)"),
                {"id": refund_id},
            )
            await conn.execute(
                text("DELETE FROM allocations WHERE invoice_id = CAST(:id AS uuid)"),
                {"id": invoice_id},
            )
            await conn.execute(
                text("DELETE FROM refunds WHERE id = CAST(:id AS uuid)"), {"id": refund_id}
            )
            await conn.execute(
                text("DELETE FROM invoices WHERE id = CAST(:id AS uuid)"), {"id": invoice_id}
            )
            await conn.execute(
                text("DELETE FROM applications WHERE id = CAST(:id AS uuid)"),
                {"id": application_id},
            )
            await conn.execute(
                text("DELETE FROM applicants WHERE id = CAST(:id AS uuid)"),
                {"id": applicant_id},
            )
            await conn.execute(
                text("DELETE FROM users WHERE id = CAST(:id AS uuid)"), {"id": user_id}
            )


async def test_0045_downgrade_survives_a_committed_invoice_recipients_row(engine):
    """Minor 8 (whole-branch review): `0045`'s downgrade has two bugs
    already found BY HAND — narrowing `allocations.target_valid` before
    collapsing `'receiver'` rows back to `'budget'`, and dropping
    `invoice_recipients` while a real FK still pointed at the seeded
    budget recipient — both fixed at the time (task 4/5 fixes, this
    module's own docstring), and both untestable by
    `test_downgrade_upgrade_roundtrip` alone: that test always runs
    base -> head -> base -> head on an EMPTY database, so neither the
    CHECK nor the FK it narrows/drops against ever has a row to choke on.

    This test is `test_0046_downgrade_survives_a_returned_refund_with_
    real_components`'s own mirror, one migration further down: a REAL,
    COMMITTED `invoice_recipients` row (the split frozen at issuance,
    naming the seeded budget recipient) and a REAL `target='receiver'`
    allocation, then a downgrade all the way to `"0044"` — past BOTH
    `0046` (which first collapses `'receiver'`/`recipient_id` back onto
    the legacy `'budget'` shape) and `0045` (which drops
    `invoice_recipients` and the `recipient_id` column outright). Proves
    the two-migration path a real committed row actually travels, not
    just each migration's own isolated downgrade."""
    url = get_settings().database_url_test
    cfg = _alembic_config(url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    user_id = uuid.uuid4()
    applicant_id = uuid.uuid4()
    application_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    pinfl = str(applicant_id.int % 10**14).zfill(14)

    async with engine.begin() as conn:
        role_id = (await conn.execute(text("SELECT id FROM roles LIMIT 1"))).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, full_name, role_id, status, must_change_password, failed_login_count) "
                "VALUES (CAST(:id AS uuid), 'Migration test user', "
                "CAST(:role_id AS uuid), 'active', false, 0)"
            ),
            {"id": user_id, "role_id": role_id},
        )
        await conn.execute(
            text(
                "INSERT INTO applicants (id, kind, pinfl, name, owner_user_id) "
                "VALUES (CAST(:id AS uuid), 'individual', :pinfl, "
                "'Migration test applicant', CAST(:user_id AS uuid))"
            ),
            {"id": applicant_id, "pinfl": pinfl, "user_id": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO applications "
                "(id, applicant_id, submitted_by_user_id, on_behalf, channel, status, kind) "
                "VALUES (CAST(:id AS uuid), CAST(:applicant_id AS uuid), "
                "CAST(:user_id AS uuid), 'self', 'portal', 'APPROVED', 'new')"
            ),
            {"id": application_id, "applicant_id": applicant_id, "user_id": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO invoices (id, number, application_id, amount, status) "
                "VALUES (CAST(:id AS uuid), :number, CAST(:application_id AS uuid), "
                "600000.00, 'paid')"
            ),
            {
                "id": invoice_id,
                "number": f"TEST-0045-{invoice_id.hex[:12]}",
                "application_id": application_id,
            },
        )
        # The split FROZEN at issuance (decision #158): the seeded budget
        # recipient at position 0, the leshoz's own remainder last — the
        # exact shape `issue_invoice`/`_snapshot_rows` writes for real.
        await conn.execute(
            text(
                "INSERT INTO invoice_recipients "
                "(id, invoice_id, recipient_id, name, kind, percent, amount, position) "
                "VALUES (gen_random_uuid(), CAST(:invoice_id AS uuid), "
                "CAST(:recipient_id AS uuid), "
                "CAST(:name AS jsonb), 'percent', 50.00, 300000.00, 0)"
            ),
            {
                "invoice_id": invoice_id,
                "recipient_id": _BUDGET_RECIPIENT_ID,
                "name": '{"uz_latn": "Davlat byudjeti"}',
            },
        )
        await conn.execute(
            text(
                "INSERT INTO invoice_recipients "
                "(id, invoice_id, recipient_id, name, kind, amount, position) "
                "VALUES (gen_random_uuid(), CAST(:invoice_id AS uuid), NULL, "
                "CAST(:name AS jsonb), 'remainder', 300000.00, 1)"
            ),
            {"invoice_id": invoice_id, "name": '{"uz_latn": "Leshoz"}'},
        )
        # The ledger row the same payment actually wrote (`ledger.
        # entries_for_shares`): `target='receiver'`, `recipient_id` set —
        # the exact shape `0046`'s downgrade must fold back onto
        # `target='budget'`, `recipient_id=NULL` BEFORE `0045`'s own
        # downgrade drops the column entirely.
        await conn.execute(
            text(
                "INSERT INTO allocations "
                "(id, invoice_id, recipient_id, entry_type, target, amount) "
                "VALUES (gen_random_uuid(), CAST(:invoice_id AS uuid), "
                "CAST(:recipient_id AS uuid), 'payment', 'receiver', 300000.00)"
            ),
            {"invoice_id": invoice_id, "recipient_id": _BUDGET_RECIPIENT_ID},
        )

    try:
        # THE PROOF: this must not raise. Before the task 4/5 fixes, either
        # `0046`'s FK-narrowing-before-collapse or `0045`'s drop-before-
        # collapse ordering would abort here against exactly this shape of
        # committed row.
        await asyncio.to_thread(command.downgrade, cfg, "0044")

        async with engine.connect() as conn:
            # `invoice_recipients` does not survive past `0045` — the table
            # itself is gone, dropped along with the row this test committed.
            exists = (
                await conn.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                        "WHERE table_name = 'invoice_recipients')"
                    )
                )
            ).scalar_one()
            assert exists is False

            # `allocations.recipient_id` does not survive past `0045` either.
            has_column = (
                await conn.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'allocations' AND column_name = 'recipient_id')"
                    )
                )
            ).scalar_one()
            assert has_column is False

            # What comes back: the `'receiver'` row `0046`'s downgrade folded
            # onto `'budget'` stays `'budget'` through `0045`'s own downgrade
            # too (that migration's `UPDATE ... WHERE target = 'receiver'` is
            # a no-op by the time it runs — nothing to do, and nothing to
            # break).
            row = (
                await conn.execute(
                    text("SELECT target FROM allocations WHERE invoice_id = CAST(:id AS uuid)"),
                    {"id": invoice_id},
                )
            ).one()
            assert row.target == "budget"
    finally:
        # Same reasoning as the 0046 test above: leave the schema at head
        # regardless of outcome, and clean up every row this test
        # committed — `allocations` (unaffected by either DROP TABLE, so
        # still here after the upgrade back), `invoice_recipients`
        # (recreated EMPTY by the upgrade, nothing of this test's own to
        # delete there), then the FK chain beneath the invoice.
        await asyncio.to_thread(command.upgrade, cfg, "head")
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM allocations WHERE invoice_id = CAST(:id AS uuid)"),
                {"id": invoice_id},
            )
            await conn.execute(
                text("DELETE FROM invoices WHERE id = CAST(:id AS uuid)"), {"id": invoice_id}
            )
            await conn.execute(
                text("DELETE FROM applications WHERE id = CAST(:id AS uuid)"),
                {"id": application_id},
            )
            await conn.execute(
                text("DELETE FROM applicants WHERE id = CAST(:id AS uuid)"),
                {"id": applicant_id},
            )
            await conn.execute(
                text("DELETE FROM users WHERE id = CAST(:id AS uuid)"), {"id": user_id}
            )


async def test_downgrade_upgrade_roundtrip(engine):
    """upgrade head → downgrade base → upgrade head (plan 03.4 ruling 16).

    KEEP THIS TEST LAST IN THIS FILE (this file's ordering is guaranteed by the
    collection hook in tests/conftest.py, not by alphabetical order):
    downgrade base wipes the shared test DB — every table is dropped and
    re-created; data other tests created is gone. Nothing may run after it."""
    url = get_settings().database_url_test
    cfg = _alembic_config(url)
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await asyncio.to_thread(command.downgrade, cfg, "base")
    await asyncio.to_thread(command.upgrade, cfg, "head")
    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    # The head is pinned as a literal on purpose: a second head is invisible to
    # every other test (conftest's migrator is session-scoped and autouse, so a
    # branch point kills the whole suite rather than one case). Move this in the
    # SAME commit as the migration that moves the head.
    assert version == "0046"
