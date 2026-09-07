"""prosecutor reads everything

Decision #95 (Oybek, 2026-09-06), answering `tz/12` #48: the prosecutor's surface
is read-only over the WHOLE system, not a subset.

What was found (stage 7.3 walkthrough, finding F20): `demo_prosecutor` read
applications and permits fine and got `403 ERR-ACL-001
{"permission":"payments.view"}` on `GET /payments/allocations`, while `tz/04` С22
lists «платежи (инвойсы, транзакции, распределение, сверка, refund)» among the
things the prosecutor inspects. Half of what oversight exists to look at was
invisible to it.

Three read permissions were missing; every other read code in the system was
already granted (`applications.view_any`, `permits.view_any`,
`inspections.view_any`, `oversight.view`, `reports.view`, `signatures.view_any`,
`search.use`, `dashboard.view`). What each of the three unlocks, so the grant is
reviewable rather than a list of strings:

  * `payments.view` — the invoice register and one invoice, allocations,
    reconciliations, bank statements and refunds, all read-only. This is С22's
    own list.
  * `auth.users.view` — the staff directory (`GET /admin/users*`, read-only
    routes only; `auth.users.manage` is what writes and is NOT granted). С22
    filters by «ответственный», which is a user.
  * `admin.integrations.view` — the outbox and DLQ registers. `payload` is
    stripped from every listing by `admin/integrations_router.py`, so this is
    queue metadata and carries no personal data; requeueing is
    `admin.integrations.manage` and is NOT granted.

**Not granted, deliberately: `archive.manage`.** Its description is "Archive a
terminal application or permit, AND read the register" — one code for a read and
a write, so a read-only role cannot hold the read without the write. Splitting it
belongs to the archive work (stage 6.9), not to a grant migration; recorded in
`plans/07.3-findings.md`.

Every code here is a READ. Decision #95's standing rule: a module that adds a
read permission adds it to `prosecutor` in the same migration; a write permission
never reaches this role.

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-06 18:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_CODE = "prosecutor"

# Read-only codes only. Each is registered by its owning module
# (`auth/permissions.py`, `payments/permissions.py`, `admin/permissions.py`).
GRANTS: tuple[str, ...] = (
    "payments.view",
    "auth.users.view",
    "admin.integrations.view",
)


def upgrade() -> None:
    for permission_code in GRANTS:
        op.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_code)"
                " SELECT id, :perm FROM roles WHERE code = :role"
                " ON CONFLICT DO NOTHING"
            ).bindparams(perm=permission_code, role=ROLE_CODE)
        )


def downgrade() -> None:
    # Scoped to THIS role: the same codes are held by accountant, sys_admin and
    # central_admin, and a `WHERE permission_code IN (...)` alone — the shape the
    # sibling migrations use, where the codes are new — would revoke them from
    # everyone.
    for permission_code in GRANTS:
        op.execute(
            sa.text(
                "DELETE FROM role_permissions"
                " WHERE permission_code = :perm"
                " AND role_id = (SELECT id FROM roles WHERE code = :role)"
            ).bindparams(perm=permission_code, role=ROLE_CODE)
        )
