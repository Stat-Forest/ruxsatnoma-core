"""approver role alignment

Moves the right to APPROVE from `leadership` to `executor_head`, per `tz/03`'s
permission matrix (4-илова) and Oybek's decision of 2026-09-02 (decision #59,
option а; the question was filed as `tz/12` #14 by stage 3.9a-core).

The matrix gives «Т» — утверждение/подпись — on every object to the column
«Раҳбар», which `0003_auth` seeds as **`executor_head`** («Ваколатли шахс», the
leshoz head). «Руководство» — `leadership`, «Агентлик раҳбарияти» — holds «К,Э»,
view and export, and nothing more. `backend/.claude/lessons.md` had asserted
since 3.6a that "the rahbar's role code is `leadership`", conflating the two
columns, so migrations 0010 and 0011 granted approval to `leadership` and
`executor_head` ended up unable to approve a contour or a norm in its own
leshoz. 0015 half-corrected it by granting `applications.decide` to both.

Decision #29's escalation ladder (лесхоз → тер. управление → агентство) needs no
help from `leadership`: each level has its own `executor_head`, reached through
the organization hierarchy, so removing the grant closes the gap rather than
opening one.

Deliberately NOT touched, both parked as separate questions in `tz/12` #14:
  * `leadership` KEEPS `norms.publish`. Stage 3.7 ruling 16 put publication with
    the central office because ВМҚ 689 has forest-pasture norms approved at the
    level of the forestry authority, not the leshoz — agency leadership is a
    defensible holder there, unlike approval of an individual contour or norm.
  * `chief_forester` KEEPS `gis.contours.approve`. «Бош ўрмонбеги» has no column
    in `tz/03`'s matrix at all (ten columns for eleven seeded roles), so there is
    nothing to align it against yet.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-02 07:14:02.118374

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The approval codes that move. `applications.decide` is a revoke only —
# `executor_head` already holds it from 0015.
REVOKE_FROM_LEADERSHIP: tuple[str, ...] = (
    "gis.contours.approve",
    "norms.approve",
    "applications.decide",
)
GRANT_TO_EXECUTOR_HEAD: tuple[str, ...] = (
    "gis.contours.approve",
    "norms.approve",
)


def _grant(role: str, code: str) -> None:
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_code) "
            "SELECT id, :code FROM roles WHERE code = :role "
            "ON CONFLICT DO NOTHING"
        ).bindparams(code=code, role=role)
    )


def _revoke(role: str, code: str) -> None:
    op.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_code = :code "
            "AND role_id = (SELECT id FROM roles WHERE code = :role)"
        ).bindparams(code=code, role=role)
    )


def upgrade() -> None:
    """Upgrade schema."""
    for code in GRANT_TO_EXECUTOR_HEAD:
        _grant("executor_head", code)
    for code in REVOKE_FROM_LEADERSHIP:
        _revoke("leadership", code)


def downgrade() -> None:
    """Downgrade schema.

    Restores exactly what 0010, 0011 and 0015 had granted — `leadership` gets its
    three codes back and `executor_head` loses the two this revision added. It
    does NOT touch `executor_head`'s `applications.decide`, which came from 0015
    and is that revision's to remove.
    """
    for code in REVOKE_FROM_LEADERSHIP:
        _grant("leadership", code)
    for code in GRANT_TO_EXECUTOR_HEAD:
        _revoke("executor_head", code)
