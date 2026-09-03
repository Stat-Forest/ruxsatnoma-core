"""merge 3.10a payments (0018) and 3.11a permits (0020)

Empty on purpose: this revision creates nothing and drops nothing. It exists only
to give the two chains that both branched off `0016` a single head again —
`0017`/`0018` (3.10a payments) and `0019`/`0020` (3.11a permits), the two blocks
`plans/03.9-3.11-parallel-run.md` reserved for the parallel run.

**Why a merge and not a renumber.** Rewriting `down_revision` (or renaming `0019`
to `0021`) breaks every environment that already applied the originals — the dev
DB, three worktrees' test DBs and CI all hold `0020` or `0018` in
`alembic_version` and would never reach the renamed script (lesson: "Multiple
Alembic heads: resolve with an empty merge migration"). `alembic merge heads` is
the only correct answer once a revision has been applied anywhere.

**Why the id is not a number.** `plans/03.9-3.11-parallel-run.md` reserves `0021`
for 3.9b, `0022` for 3.10b, `0023` for 3.11b and `0024` for 3.9a-flow. Taking
`0021` here would steal a number a session is already building against, so this
revision is named rather than numbered and those four stay free — each of them
sets its `down_revision` to `merge_0018_0020`.

The two chains were verified disjoint before merging: no shared table, index,
constraint or trigger name, and the seeded rows do not collide either — the
permission codes (`payments.*` vs `permits.*`) and the notification event codes
(`invoice.due_soon` vs `permit.*`) are value-disjoint, so neither chain's seed
trips the other's partial unique index.

Revision ID: merge_0018_0020
Revises: 0018, 0020
Create Date: 2026-09-03 02:55:24.868282

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "merge_0018_0020"
down_revision: str | Sequence[str] | None = ("0018", "0020")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do — the two chains are independent (see the module docstring)."""


def downgrade() -> None:
    """Nothing to undo — `upgrade` created nothing."""
