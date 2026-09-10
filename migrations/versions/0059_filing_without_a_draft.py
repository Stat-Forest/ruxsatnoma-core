"""filing without a draft

Stage 12 (docs/plans/12-filing-without-a-draft.md, rulings R7/R8): `DRAFT`
leaves `applications.status`. An application is created by one request and
is born SUBMITTED (`applications.service.file`); nothing earlier than that
exists any more.

Two things happen here, in this order:

1. **The drafts that exist are DELETED**, with everything that hangs off
   them (R8) — items, documents, checks, assignments, info requests,
   conclusions, the calculations a draft's owner may have priced (R16 let
   them; `calculations` is append-only, so its trigger is stood down for
   the one statement, 0023's own tool — `DISABLE TRIGGER USER`, never
   `ALL`, which needs a superuser), and their `DRAFT` history rows (same
   tool). An inspection task or act that named a draft keeps its row and
   loses the reference (the column is nullable). `audit_log` keeps whatever
   it says about them. Option (б), moving them to CANCELLED, was declined:
   a numberless cancelled application in a citizen's list is the very
   garbage this stage exists to remove.
2. **`ck_applications_status_valid` is rebuilt without `DRAFT`.** The two
   CHECKs on `application_status_history` are NOT touched (R7): the table
   is append-only and every application filed before this stage has a
   `DRAFT -> SUBMITTED` row; `models.HISTORY_STATUSES` is that table's
   vocabulary, `APPLICATION_STATUSES` the live one.

Renumbered twice on its way to `dev` — `0056` when `0056`/`0057` landed, `0059` when
`0058_statutory_exemption_settlement` (#202) merged five minutes ahead of it: the
lessons.md "same id, not a splice" case, re-pointed by hand because nothing had run it
anywhere yet.

The downgrade restores the CHECK; the deleted drafts do not come back —
there is nothing to restore them from, and a downgrade must say so rather
than pretend.

Revision ID: 0059
Revises: 0058
Create Date: 2026-09-11 02:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0059"
down_revision: str | Sequence[str] | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = (
    "SUBMITTED",
    "IN_REVIEW",
    "PENDING_INFO",
    "RETURNED",
    "APPROVED",
    "INVOICED",
    "PAID",
    "PERMIT_ISSUED",
    "REJECTED",
    "CANCELLED",
    "EXPIRED_UNPAID",
    "CLOSED",
    "ARCHIVED",
)
CHECK = "status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")"
OLD_CHECK = "status IN ('DRAFT', " + ", ".join(f"'{s}'" for s in STATUSES) + ")"
DRAFTS = "SELECT id FROM applications WHERE status = 'DRAFT'"


def upgrade() -> None:
    """Upgrade schema."""
    # 1. The drafts and their dependants, children first.
    op.execute("ALTER TABLE calculations DISABLE TRIGGER USER")
    op.execute(f"DELETE FROM calculations WHERE application_id IN ({DRAFTS})")
    op.execute("ALTER TABLE calculations ENABLE TRIGGER USER")
    for table in (
        "application_checks",
        "application_documents",
        "application_items",
        "application_assignments",
        "info_requests",
        "application_conclusions",
    ):
        op.execute(f"DELETE FROM {table} WHERE application_id IN ({DRAFTS})")
    for table in ("inspection_tasks", "inspection_acts"):
        op.execute(f"UPDATE {table} SET application_id = NULL WHERE application_id IN ({DRAFTS})")
    op.execute("ALTER TABLE application_status_history DISABLE TRIGGER USER")
    op.execute(f"DELETE FROM application_status_history WHERE application_id IN ({DRAFTS})")
    op.execute("ALTER TABLE application_status_history ENABLE TRIGGER USER")
    op.execute("DELETE FROM applications WHERE status = 'DRAFT'")
    # 2. The vocabulary.
    # `op.f()`: 0015 created the CHECK under this exact name; without the
    # wrapper the naming convention would prefix it a second time
    # (`ck_applications_ck_applications_status_valid`, seen on the first run).
    op.drop_constraint(op.f("ck_applications_status_valid"), "applications", type_="check")
    op.create_check_constraint(op.f("ck_applications_status_valid"), "applications", CHECK)


def downgrade() -> None:
    """Downgrade schema — the CHECK only; the deleted drafts are gone."""
    op.drop_constraint(op.f("ck_applications_status_valid"), "applications", type_="check")
    op.create_check_constraint(op.f("ck_applications_status_valid"), "applications", OLD_CHECK)
