"""benefit_proof_doc_type

The ONE `doc_types` classifier item a benefit claim is proven with —
`applications.service.BENEFIT_DOC_TYPE_CODE`, ruling 10а of plan
`03.9a-applications-core`, made fail-CLOSED by branch 2's task 5.

**Why a migration seeds it at all.** Migration `0005_admin_seeds.py` created the
`doc_types` classifier with no items: they are the Agency's to supply, and no
`tz/` document prescribes a code list for them. But `_assert_benefit_documents`
looks this ONE code up by name and refuses every benefit claim while it is
missing, so shipping the guard without the row would say
«имтиёзни тасдиқловчи ҳужжат тури созланмаган» to a citizen whose benefit is
perfectly real — turning "benefit claims are refused until the Agency answers"
into "benefit claims are refused because we forgot a row". Those two states
must never be confused, so the code is OURS and this migration owns it. The
remaining document types stay the Agency's, added through the admin CRUD.

Nothing else changes: benefit claims are still fail-closed on the OTHER half —
`benefit_categories` is empty until VMQ 278's list arrives (`tz/12` #2/#13), so
an applicant cannot pick a category to prove in the first place.

`0024` is this stage's reserved revision number
(`plans/03.9-3.11-parallel-run.md`). `down_revision` is `merge_0018_0020` — an
ID, not a number: `dev` forked at `0016` into `0017`/`0018` (3.10a) and
`0019`/`0020` (3.11a) and an empty merge revision rejoined them, because
renumbering would have stranded every database that had already applied the
originals (lesson).

Revision ID: 0024
Revises: merge_0018_0020
Create Date: 2026-09-03 12:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | Sequence[str] | None = "merge_0018_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# A fixed id, `0005_admin_seeds.py`'s own idiom for a classifier item, so the
# downgrade can name exactly the row the upgrade wrote and no other.
ITEM_ID = "0198f100-0024-7000-8000-000000000001"
CLASSIFIER_CODE = "doc_types"
# `applications.service.BENEFIT_DOC_TYPE_CODE`, as a literal: a migration is a
# frozen historical statement and must not change meaning when the constant is
# renamed. `tests/modules/applications/test_documents.py` holds the two
# together.
ITEM_CODE = "benefit_proof"


def upgrade() -> None:
    """Seed the item, and say so out loud in both directions it could go quiet.

    A plain `INSERT ... SELECT FROM classifiers WHERE code = 'doc_types'` has
    two silent modes, and this project has a lesson about exactly the first:
    with the classifier absent the SELECT yields no row, the INSERT writes
    nothing, and the migration reports success — the "empty migration, no
    error, no warning" shape. And against a database where a central admin has
    already created an active `benefit_proof` item through the admin CRUD, the
    partial index `uq_classifier_items_active_code` turns the seed into a hard
    crash mid-deploy over a row that is exactly what we wanted there.

    So: the classifier is resolved first and its absence RAISES with a sentence
    naming it; the insert is `ON CONFLICT DO NOTHING` against that index, so an
    admin-created row is left alone rather than colliding; and the outcome is
    verified, so "nothing was inserted and nothing exists" can never pass for
    "already seeded".
    """
    conn = op.get_bind()
    classifier_id = conn.execute(
        sa.text("SELECT id FROM classifiers WHERE code = :code").bindparams(code=CLASSIFIER_CODE)
    ).scalar()
    if classifier_id is None:
        raise RuntimeError(
            f"classifier {CLASSIFIER_CODE!r} is missing — 0005_admin_seeds.py seeds it, so this "
            "database did not run the chain this revision depends on; seeding nothing here would "
            "leave every benefit claim refused as 'benefit_doc_type_not_configured'"
        )
    conn.execute(
        sa.text(
            "INSERT INTO classifier_items "
            "(id, classifier_id, code, name, valid_from, sort_order, status) VALUES "
            "(CAST(:id AS uuid), CAST(:classifier_id AS uuid), :code, "
            "jsonb_build_object('uz_cyrl', :cyr, 'ru', :ru, 'en', :en), "
            "DATE '2026-01-01', 10, 'active') "
            # The index is PARTIAL, so the conflict target must repeat its own
            # WHERE clause or Postgres cannot infer it.
            "ON CONFLICT (classifier_id, code) WHERE status = 'active' DO NOTHING"
        ).bindparams(
            id=ITEM_ID,
            classifier_id=classifier_id,
            code=ITEM_CODE,
            cyr="Имтиёзни тасдиқловчи ҳужжат",
            ru="Документ, подтверждающий льготу",
            en="Benefit proof",
        )
    )
    seeded = conn.execute(
        sa.text(
            "SELECT count(*) FROM classifier_items "
            "WHERE classifier_id = CAST(:classifier_id AS uuid) "
            "AND code = :code AND status = 'active'"
        ).bindparams(classifier_id=classifier_id, code=ITEM_CODE)
    ).scalar()
    if not seeded:
        raise RuntimeError(
            f"no active {ITEM_CODE!r} item exists after this migration — the insert was skipped "
            "by ON CONFLICT and nothing was there to conflict with, which cannot both be true"
        )


def downgrade() -> None:
    # The attachments FIRST. `application_documents.doc_type_item_id` is an FK
    # to this row, and the moment anything references a seeded item the plain
    # delete below fails on it — which is how `0010`'s template seed broke the
    # downgrade→upgrade round-trip in a task that touched no migration
    # (lesson: "a downgrade must delete whatever its upgrade made possible").
    # Downgrade-only data loss of attachments whose TYPE is being removed; the
    # upgrade path is untouched.
    op.execute(
        sa.text(
            "DELETE FROM application_documents WHERE doc_type_item_id = CAST(:id AS uuid)"
        ).bindparams(id=ITEM_ID)
    )
    # By ID, never by code: on a database where the upgrade's ON CONFLICT left
    # an ADMIN-created row alone, this migration inserted nothing and owns
    # nothing, so a delete by code would remove somebody else's row.
    op.execute(
        sa.text("DELETE FROM classifier_items WHERE id = CAST(:id AS uuid)").bindparams(id=ITEM_ID)
    )
