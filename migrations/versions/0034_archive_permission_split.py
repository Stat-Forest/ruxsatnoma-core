"""archive permission split

F23 (`docs/plans/07.3-findings.md`, stage 6.9): `archive.manage`'s own registered
description was "Archive a terminal application or permit, AND read the register"
— one code for a read and a write, so a read-only role could never hold the read
without also getting the write. Decision #95 gives the prosecutor every READ in
the system; `0033_prosecutor_reads.py` deliberately left `archive.manage`
ungranted for exactly this reason.

Splits it into `archive.view` (read the register, read one item) and
`archive.manage` (archive an object, verify a stored item) — see
`app/modules/archive/permissions.py` and the router change beside it. No schema
change; this migration only grants the new code.

`archive.view` goes to every role that already holds `archive.manage`
(`central_admin`, `executor_head`, per `0029_search_archive.py`) so nobody loses
an ability, plus `prosecutor` so decision #95 finally holds for this module too
(`tests/test_permissions_registry.py::test_the_prosecutor_holds_every_read_permission`
enforces this mechanically for every `.view`/`.view_any` code going forward).

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-07 01:18:25.684638

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0034"
down_revision: str | Sequence[str] | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same roles `0029_search_archive.py` granted `archive.manage` to, plus the
# prosecutor (decision #95).
ARCHIVE_VIEW_ROLES: tuple[str, ...] = ("central_admin", "executor_head", "prosecutor")


def upgrade() -> None:
    for role in ARCHIVE_VIEW_ROLES:
        op.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_code) "
                "SELECT id, 'archive.view' FROM roles WHERE code = :role "
                "ON CONFLICT DO NOTHING"
            ).bindparams(role=role)
        )


def downgrade() -> None:
    op.execute("DELETE FROM role_permissions WHERE permission_code = 'archive.view'")
