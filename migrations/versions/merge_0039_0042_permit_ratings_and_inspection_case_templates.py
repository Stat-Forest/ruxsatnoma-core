"""merge permit ratings (0039) and inspection case templates (0042)

Empty on purpose: this revision creates nothing and drops nothing. It exists only
to give the two chains that both branched off `0037` a single head again —
`0038`/`0039` (7.7 services-catalog-and-ratings) and `0040`/`0041`/`0042` (5.1
SMS charset, session OneID token, 7.6 inspection case templates).

**Why a merge and not a hand-edited `down_revision`.** An earlier ruling tried to
collapse the two heads by re-pointing `0039`'s `down_revision` at `0042` and
`0040`'s at `0038`, without a merge revision. That is wrong for a revision
already applied anywhere: Alembic stores only the current head in
`alembic_version` and never walks back for an ancestor spliced in underneath an
already-applied revision. `dev` reached `0042` through the ORIGINAL
`0037 -> 0040 -> 0041 -> 0042` edge, never through `0038`/`0039` — so with the
hand-edited graph, `alembic upgrade head` on that database would apply `0039`
alone (the only revision whose `down_revision` names `0042`) and never apply
`0038`, silently leaving `activity_types.description`/`processing_days` missing
forever. See `0039`'s and `0040`'s own notes, corrected in the same commit as
this merge. `alembic merge heads` is the only correct answer once a revision has
been applied anywhere (same lesson `merge_0018_0020` already recorded).

Verified from BOTH starting points on a scratch database (see this stage's final
report): a database at `0042` applies `0038`, then `0039`, then this merge; a
database at `0039` applies `0040`, `0041`, `0042`, then this merge. Both land on
a single head, `merge_0039_0042`.

The two chains were verified disjoint before merging: no shared table, index,
constraint or trigger name (`permit_ratings` vs `notification_templates` rows
only), and the seeded/altered data do not collide either — `ratings.view` is a
new permission code, and `0042`'s four `violation_case.*` event codes are
value-disjoint from anything `0038`/`0039` touch.

Revision ID: merge_0039_0042
Revises: 0039, 0042
Create Date: 2026-09-08 00:00:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "merge_0039_0042"
down_revision: str | Sequence[str] | None = ("0039", "0042")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do — the two chains are independent (see the module docstring)."""


def downgrade() -> None:
    """Nothing to undo — `upgrade` created nothing."""
