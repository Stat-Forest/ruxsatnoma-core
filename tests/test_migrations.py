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
    just each migration's own isolated downgrade.

    The allocation names a SECOND, fabricated recipient — not the seeded
    budget one the invoice_recipients row uses. `0046`'s downgrade folds
    only rows matching `recipient_id = BUDGET_RECIPIENT_ID`; a row naming
    any other recipient sails through it untouched, still
    `target='receiver'`, and only `0045`'s OWN blanket
    `UPDATE allocations SET target = 'budget' WHERE target = 'receiver'`
    collapses it before its CHECK is narrowed back. Using the budget
    recipient here instead (as an earlier version of this test did) made
    `0046`'s downgrade collapse the row FIRST, so `0045`'s own
    collapse-then-narrow ran against nothing — a regression that swapped
    that migration's own order back would have found zero violating rows
    and passed unnoticed."""
    url = get_settings().database_url_test
    cfg = _alembic_config(url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    user_id = uuid.uuid4()
    applicant_id = uuid.uuid4()
    application_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    other_recipient_id = uuid.uuid4()
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
        # A SECOND payment_recipients row, fabricated for this test and
        # deliberately NOT the seeded budget recipient — see the docstring
        # and the comment on the allocations INSERT below for why.
        await conn.execute(
            text(
                "INSERT INTO payment_recipients (id, name, kind, percent) "
                "VALUES (CAST(:id AS uuid), CAST(:name AS jsonb), 'percent', 25.00)"
            ),
            {"id": other_recipient_id, "name": '{"uz_latn": "Boshqa oluvchi"}'},
        )
        # The ledger row the same payment actually wrote (`ledger.
        # entries_for_shares`): `target='receiver'`, `recipient_id` set —
        # but against `other_recipient_id`, NOT `_BUDGET_RECIPIENT_ID`.
        # `0046`'s own downgrade only folds rows matching
        # `recipient_id = BUDGET_RECIPIENT_ID` (its own targeted UPDATE);
        # a row naming any OTHER recipient sails through `0046`'s
        # downgrade untouched, still `target='receiver'`, and lands on
        # `0045`'s downgrade as a live row that its blanket
        # `UPDATE ... WHERE target = 'receiver'` must actually collapse
        # BEFORE the CHECK is narrowed back — the case that was a no-op
        # (and proved nothing) while this row named the budget recipient
        # instead.
        await conn.execute(
            text(
                "INSERT INTO allocations "
                "(id, invoice_id, recipient_id, entry_type, target, amount) "
                "VALUES (gen_random_uuid(), CAST(:invoice_id AS uuid), "
                "CAST(:recipient_id AS uuid), 'payment', 'receiver', 300000.00)"
            ),
            {"invoice_id": invoice_id, "recipient_id": other_recipient_id},
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

            # What comes back: `0046`'s downgrade left this row untouched
            # (its recipient_id does not match BUDGET_RECIPIENT_ID), so it
            # reaches `0045`'s downgrade still `target='receiver'` — and
            # THAT migration's own blanket
            # `UPDATE ... WHERE target = 'receiver'` is what collapses it
            # to `'budget'` here, before the CHECK is narrowed back. This
            # is the assertion this test actually exists to prove.
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


async def test_0052_downgrade_deletes_a_simple_signature_row(engine):
    """Track B1 (stage 10), ruling #183: `0052`'s downgrade must delete every
    `kind = 'simple'` row before restoring `certificate_id NOT NULL` — a
    simple row carries no certificate by construction, so the bare `ALTER
    COLUMN ... SET NOT NULL` would otherwise refuse the instant one real row
    sits in the table (lesson: a downgrade must delete whatever its upgrade
    made possible)."""
    url = get_settings().database_url_test
    cfg = _alembic_config(url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    signature_id = uuid.uuid4()
    object_id = uuid.uuid4()
    async with engine.begin() as conn:
        # No `signer_user_id`/FK chain needed at all — the column is nullable
        # and this test is about the MIGRATION's downgrade, not about what a
        # real `sign_simple()` row looks like end to end (that is
        # `tests/modules/signatures/test_simple.py`'s job).
        await conn.execute(
            text(
                "INSERT INTO signatures "
                "(id, object_type, object_id, purpose, kind, certificate_id, "
                "doc_hash, signature_value, signed_at, verification, verification_status) "
                "VALUES (CAST(:id AS uuid), 'permit', CAST(:object_id AS uuid), "
                "'permit_recipient', 'simple', NULL, "
                "'deadbeef', '', now(), '{}'::jsonb, 'valid')"
            ),
            {"id": signature_id, "object_id": object_id},
        )

    try:
        # THE PROOF: this must not raise — before a correct downgrade, restoring
        # `certificate_id NOT NULL` against this row's own NULL would abort with
        # a NotNullViolation.
        await asyncio.to_thread(command.downgrade, cfg, "0051")

        async with engine.connect() as conn:
            exists = (
                await conn.execute(
                    text("SELECT EXISTS (SELECT 1 FROM signatures WHERE id = CAST(:id AS uuid))"),
                    {"id": signature_id},
                )
            ).scalar_one()
        assert exists is False, "a simple row must not survive the downgrade to 0051"
    finally:
        # Leave the schema at head regardless of outcome — the next test in
        # this file must find it there. Nothing to clean up: the row this
        # test inserted is exactly what the downgrade deleted (or, had the
        # downgrade raised, still needs no cleanup of its own since the next
        # `upgrade head` neither restores nor duplicates it).
        await asyncio.to_thread(command.upgrade, cfg, "head")


async def test_0053_role_rename_keeps_a_users_assignment(engine):
    """Stage 10, ruling #182: `0053` renames `benefit_verifier` to
    `beekeeping_registrar` by UPDATE, same row id — a user assigned that
    role before the migration must still hold it after, because nothing
    ever touches `users.role_id`. Walked as a real downgrade→upgrade
    (`test_downgrade_upgrade_roundtrip` only ever runs base→head on an
    EMPTY database, so it never exercises this): land on `0051` (the OLD
    role name), assign a user, upgrade to `0053`, and check the SAME row
    id now reads the NEW code."""
    url = get_settings().database_url_test
    cfg = _alembic_config(url)
    # `upgrade` only ever walks FORWARD — on a DB already at/past `0053` (the
    # common case: the session migrator already brought it to head) a bare
    # `command.upgrade(cfg, "0051")` is a silent no-op, not a downgrade. Land
    # on head first, then walk back explicitly.
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await asyncio.to_thread(command.downgrade, cfg, "0051")

    user_id = uuid.uuid4()
    async with engine.begin() as conn:
        role_id = (
            await conn.execute(text("SELECT id FROM roles WHERE code = 'benefit_verifier'"))
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, full_name, role_id, status, must_change_password, failed_login_count) "
                "VALUES (CAST(:id AS uuid), 'Migration test registrar', "
                "CAST(:role_id AS uuid), 'active', false, 0)"
            ),
            {"id": user_id, "role_id": role_id},
        )

    try:
        await asyncio.to_thread(command.upgrade, cfg, "0053")
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT r.id AS role_id, r.code FROM users u "
                        "JOIN roles r ON r.id = u.role_id WHERE u.id = CAST(:id AS uuid)"
                    ),
                    {"id": user_id},
                )
            ).one()
        assert row.role_id == role_id
        assert row.code == "beekeeping_registrar"
    finally:
        await asyncio.to_thread(command.upgrade, cfg, "head")
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE id = CAST(:id AS uuid)"), {"id": user_id}
            )


async def test_0061_downgrade_survives_an_issued_non_grazing_permit(engine):
    """Cross-task finding: `0061`'s downgrade used to unconditionally `DELETE`
    the five rows it seeded. `pytest -n4 --fresh-db`'s full-suite run issues
    real permits on the non-grazing bundled blanks (`test_issue.py` and others)
    before `test_downgrade_upgrade_roundtrip` runs last on that worker, so the
    DELETE hit `fk_permits_template_id_permit_templates` and aborted the whole
    downgrade mid-transaction — which then failed every migration test after it
    on that worker, collaterally.

    The fix: a template row referenced by a `permits` row is ARCHIVED, not
    deleted, on downgrade — the same "frozen at issuance" reasoning every other
    permit field already gets. This test builds one permit on the seeded
    deadwood template (`0198f150-...004`), downgrades to `0062` (one step back)
    and proves the row SURVIVES (archived, permit unaffected), then upgrades
    back to head and proves the upsert reactivates that exact row rather than
    colliding with it on its own fixed id — no second active row for deadwood."""
    url = get_settings().database_url_test
    cfg = _alembic_config(url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    template_id = uuid.UUID("0198f150-0015-7000-8000-000000000004")  # deadwood, seeded by 0061
    deadwood_activity_id = uuid.UUID("0198f100-0001-7000-8000-000000000005")

    agency_id = uuid.uuid4()
    org_id = uuid.uuid4()
    contour_id = uuid.uuid4()
    version_id = uuid.uuid4()
    user_id = uuid.uuid4()
    applicant_id = uuid.uuid4()
    application_id = uuid.uuid4()
    permit_id = uuid.uuid4()
    pinfl = str(applicant_id.int % 10**14).zfill(14)

    async with engine.begin() as conn:
        # Only what the FK chain and this table's own NOT NULL columns demand —
        # 0046/0053's own principle. `organizations`/`contours`/`contour_versions`
        # are the one addition those two tests never needed: a permit carries
        # its OWN contour/organization, not the application's.
        role_id = (await conn.execute(text("SELECT id FROM roles LIMIT 1"))).scalar_one()
        layer_id = (
            await conn.execute(text("SELECT id FROM gis_layers WHERE code = 'contours'"))
        ).scalar_one()

        # The agency is a singleton (`uq_organizations_single_agency`) another
        # test module may already have committed to this shared, persistent
        # database — reuse it if so, create it otherwise (the same get-or-create
        # `tests/modules/gis/conftest.py::_agency` uses).
        existing_agency = (
            await conn.execute(text("SELECT id FROM organizations WHERE kind = 'agency' LIMIT 1"))
        ).scalar_one_or_none()
        created_agency = existing_agency is None
        if created_agency:
            await conn.execute(
                text(
                    "INSERT INTO organizations (id, kind, code, name, status) "
                    "VALUES (CAST(:id AS uuid), 'agency', :code, "
                    "'{\"uz_latn\": \"Test Agency\"}'::jsonb, 'active')"
                ),
                {"id": agency_id, "code": f"TESTAG{agency_id.hex[:8]}"},
            )
        else:
            agency_id = existing_agency

        await conn.execute(
            text(
                "INSERT INTO organizations (id, parent_id, kind, code, name, status) "
                "VALUES (CAST(:id AS uuid), CAST(:parent_id AS uuid), 'leshoz', :code, "
                "'{\"uz_latn\": \"Test Leshoz\"}'::jsonb, 'active')"
            ),
            {"id": org_id, "parent_id": agency_id, "code": f"TESTLH{org_id.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO contours (id, layer_id, organization_id, kind, number, status) "
                "VALUES (CAST(:id AS uuid), CAST(:layer_id AS uuid), CAST(:org_id AS uuid), "
                "'contour', :number, 'active')"
            ),
            {
                "id": contour_id,
                "layer_id": layer_id,
                "org_id": org_id,
                "number": f"T{contour_id.hex[:8]}",
            },
        )
        # `geom` stays NULL (decision #178: a leshoz with no delivered GIS
        # layer) — `declared_area_ha` is what `geom_or_declared_area` then
        # requires instead, and this test's downgrade is about `permit_templates`,
        # not geometry.
        await conn.execute(
            text(
                "INSERT INTO contour_versions "
                "(id, contour_id, version_no, area_ha, declared_area_ha, source, status) "
                "VALUES (CAST(:id AS uuid), CAST(:contour_id AS uuid), 1, 12.5, 12.5, "
                "'survey', 'draft')"
            ),
            {"id": version_id, "contour_id": contour_id},
        )
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
                "INSERT INTO permits "
                "(id, series, number, application_id, applicant_id, activity_type_id, "
                "organization_id, contour_id, contour_version_id, area_ha, period_from, "
                "period_to, amount, status, qr_token, template_id, snapshot) "
                "VALUES (CAST(:id AS uuid), 'Z', :number, CAST(:application_id AS uuid), "
                "CAST(:applicant_id AS uuid), CAST(:activity_id AS uuid), CAST(:org_id AS uuid), "
                "CAST(:contour_id AS uuid), CAST(:version_id AS uuid), 12.5000, "
                "'2027-05-01', '2027-09-30', 500000.00, 'active', :qr_token, "
                "CAST(:template_id AS uuid), '{}'::jsonb)"
            ),
            {
                "id": permit_id,
                "number": permit_id.int % 900000 + 100000,
                "application_id": application_id,
                "applicant_id": applicant_id,
                "activity_id": deadwood_activity_id,
                "org_id": org_id,
                "contour_id": contour_id,
                "version_id": version_id,
                "qr_token": f"test-0061-{permit_id.hex[:16]}",
                "template_id": template_id,
            },
        )

    try:
        # THE PROOF: this must not raise — before the fix, the unconditional
        # DELETE hit fk_permits_template_id_permit_templates right here.
        await asyncio.to_thread(command.downgrade, cfg, "0062")

        async with engine.connect() as conn:
            template_status = (
                await conn.execute(
                    text("SELECT status FROM permit_templates WHERE id = CAST(:id AS uuid)"),
                    {"id": template_id},
                )
            ).scalar_one()
            assert template_status == "archived"

            permit_template = (
                await conn.execute(
                    text("SELECT template_id FROM permits WHERE id = CAST(:id AS uuid)"),
                    {"id": permit_id},
                )
            ).scalar_one()
            assert permit_template == template_id, "the permit must keep pointing at its own layout"
    finally:
        await asyncio.to_thread(command.upgrade, cfg, "head")

        async with engine.connect() as conn:
            active_rows = (
                await conn.execute(
                    text(
                        "SELECT id, status FROM permit_templates "
                        "WHERE activity_type_id = CAST(:activity_id AS uuid) AND status = 'active'"
                    ),
                    {"activity_id": deadwood_activity_id},
                )
            ).all()
            assert [row.id for row in active_rows] == [template_id], (
                "the re-upgrade must reactivate the SAME row, not insert a second one"
            )

        # This test's own rows would otherwise sit in the shared per-worker
        # database forever, the same reasoning `test_0046_...`'s own cleanup
        # documents. Deleted in FK order; the agency is left alone unless this
        # run is the one that created it.
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM permits WHERE id = CAST(:id AS uuid)"), {"id": permit_id}
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
            await conn.execute(
                text("DELETE FROM contour_versions WHERE id = CAST(:id AS uuid)"),
                {"id": version_id},
            )
            await conn.execute(
                text("DELETE FROM contours WHERE id = CAST(:id AS uuid)"), {"id": contour_id}
            )
            await conn.execute(
                text("DELETE FROM organizations WHERE id = CAST(:id AS uuid)"), {"id": org_id}
            )
            if created_agency:
                await conn.execute(
                    text("DELETE FROM organizations WHERE id = CAST(:id AS uuid)"),
                    {"id": agency_id},
                )


async def test_0064_downgrade_survives_a_rejected_application_with_grounds_and_a_stored_notice(
    engine,
):
    """Stage 16 fix wave F6: `0064`'s downgrade drops
    `application_rejection_grounds`/`application_printouts` outright, then
    nulls `application_status_history.reason_item_id` and `applications.
    rejection_reason_item_id` — disabling the history table's own append-only
    trigger for that one UPDATE (lesson: an append-only referrer blocks even
    the nulling UPDATE) — before deleting the eight R01..R08 classifier rows
    those columns may point at. Untested on real data before this fix wave.

    Built by the minimal SQL `test_0061_downgrade_survives_an_issued_non_
    grazing_permit`'s own precedent uses (`test_0046_...`/`test_0045_...`
    before it): a REJECTED application whose `rejection_reason_item_id`
    names R01, a matching `application_status_history` row, one
    `application_rejection_grounds` row, and a `rejection_notice` printout
    whose PDF was ALREADY stored (`file_id`/`sha256` set — the lazy-render
    path having already run once). `downgrade` to `0061` and `upgrade` back
    to head must both succeed."""
    url = get_settings().database_url_test
    cfg = _alembic_config(url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    r01_id = uuid.UUID("0198f160-0016-7000-8000-000000000001")  # R01, seeded by 0064

    agency_id = uuid.uuid4()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    applicant_id = uuid.uuid4()
    application_id = uuid.uuid4()
    history_id = uuid.uuid4()
    ground_id = uuid.uuid4()
    file_id = uuid.uuid4()
    printout_id = uuid.uuid4()
    pinfl = str(applicant_id.int % 10**14).zfill(14)

    async with engine.begin() as conn:
        role_id = (await conn.execute(text("SELECT id FROM roles LIMIT 1"))).scalar_one()

        # The agency is a singleton another test module may already have
        # committed to the shared, persistent test database — reuse it if
        # so, create it otherwise (`test_0061_...`'s own get-or-create).
        existing_agency = (
            await conn.execute(text("SELECT id FROM organizations WHERE kind = 'agency' LIMIT 1"))
        ).scalar_one_or_none()
        created_agency = existing_agency is None
        if created_agency:
            await conn.execute(
                text(
                    "INSERT INTO organizations (id, kind, code, name, status) "
                    "VALUES (CAST(:id AS uuid), 'agency', :code, "
                    "'{\"uz_latn\": \"Test Agency\"}'::jsonb, 'active')"
                ),
                {"id": agency_id, "code": f"TESTAG{agency_id.hex[:8]}"},
            )
        else:
            agency_id = existing_agency

        await conn.execute(
            text(
                "INSERT INTO organizations (id, parent_id, kind, code, name, status) "
                "VALUES (CAST(:id AS uuid), CAST(:parent_id AS uuid), 'leshoz', :code, "
                "'{\"uz_latn\": \"Test Leshoz\"}'::jsonb, 'active')"
            ),
            {"id": org_id, "parent_id": agency_id, "code": f"TESTLH{org_id.hex[:8]}"},
        )
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
                "(id, applicant_id, submitted_by_user_id, on_behalf, channel, status, kind, "
                " rejection_reason_item_id, decision_basis) "
                "VALUES (CAST(:id AS uuid), CAST(:applicant_id AS uuid), CAST(:user_id AS uuid), "
                "'self', 'portal', 'REJECTED', 'new', CAST(:reason_id AS uuid), 'test basis')"
            ),
            {
                "id": application_id,
                "applicant_id": applicant_id,
                "user_id": user_id,
                "reason_id": r01_id,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO application_status_history "
                "(id, application_id, from_status, to_status, changed_by, reason_item_id) "
                "VALUES (CAST(:id AS uuid), CAST(:app_id AS uuid), 'IN_REVIEW', 'REJECTED', "
                "CAST(:user_id AS uuid), CAST(:reason_id AS uuid))"
            ),
            {"id": history_id, "app_id": application_id, "user_id": user_id, "reason_id": r01_id},
        )
        await conn.execute(
            text(
                "INSERT INTO application_rejection_grounds "
                "(id, application_id, position, reason_item_id, fact, legal_document, "
                " legal_clause, evidence, remedy, created_by) "
                "VALUES (CAST(:id AS uuid), CAST(:app_id AS uuid), 1, CAST(:reason_id AS uuid), "
                "'fact', 'doc', 'clause', 'evidence', 'remedy', CAST(:user_id AS uuid))"
            ),
            {"id": ground_id, "app_id": application_id, "reason_id": r01_id, "user_id": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO media_files "
                "(id, storage_key, filename, content_type, size_bytes, sha256, uploaded_by, "
                " status) "
                "VALUES (CAST(:id AS uuid), :key, 'notice.pdf', 'application/pdf', 123, "
                "'deadbeef', CAST(:user_id AS uuid), 'active')"
            ),
            {"id": file_id, "key": f"test-0064/{file_id.hex}", "user_id": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO application_printouts "
                "(id, application_id, kind, number, language, snapshot, file_id, sha256) "
                "VALUES (CAST(:id AS uuid), CAST(:app_id AS uuid), 'rejection_notice', :number, "
                "'uz_latn', '{}'::jsonb, CAST(:file_id AS uuid), 'deadbeef')"
            ),
            {
                "id": printout_id,
                "app_id": application_id,
                "number": f"RD-2026-{printout_id.int % 900000 + 100000}",
                "file_id": file_id,
            },
        )

    try:
        # THE PROOF: neither direction may raise.
        await asyncio.to_thread(command.downgrade, cfg, "0065")
    finally:
        await asyncio.to_thread(command.upgrade, cfg, "head")

        # This test's own rows would otherwise sit in the shared per-worker
        # database forever (`test_0061_...`'s own cleanup documents why).
        # `application_rejection_grounds`/`application_printouts` need no
        # cleanup of their own — the downgrade→upgrade cycle DROPPED and
        # RE-CREATED both tables, so our rows in them are already gone.
        # `application_status_history`'s row survives that cycle (only its
        # FK was nulled) and is append-only, so its own trigger has to be
        # disabled for this one DELETE too, the same way 0064's downgrade
        # disables it for the nulling UPDATE.
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE application_status_history DISABLE TRIGGER USER"))
            await conn.execute(
                text(
                    "DELETE FROM application_status_history WHERE application_id = "
                    "CAST(:id AS uuid)"
                ),
                {"id": application_id},
            )
            await conn.execute(text("ALTER TABLE application_status_history ENABLE TRIGGER USER"))
            await conn.execute(
                text("DELETE FROM applications WHERE id = CAST(:id AS uuid)"),
                {"id": application_id},
            )
            await conn.execute(
                text("DELETE FROM media_files WHERE id = CAST(:id AS uuid)"), {"id": file_id}
            )
            await conn.execute(
                text("DELETE FROM applicants WHERE id = CAST(:id AS uuid)"),
                {"id": applicant_id},
            )
            await conn.execute(
                text("DELETE FROM users WHERE id = CAST(:id AS uuid)"), {"id": user_id}
            )
            await conn.execute(
                text("DELETE FROM organizations WHERE id = CAST(:id AS uuid)"), {"id": org_id}
            )
            if created_agency:
                await conn.execute(
                    text("DELETE FROM organizations WHERE id = CAST(:id AS uuid)"),
                    {"id": agency_id},
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
    assert version == "0064"
