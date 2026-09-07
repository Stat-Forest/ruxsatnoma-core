"""session_oneid_token

The OneID access token, kept for the length of one browser session so that our
own logout can call the provider's `one_log_out` (design/04 §1.2 step 4,
decision #140 ruling 4).

Without it our logout closes only OUR session: the OneID session survives in
the browser, and on a shared computer the next person presses "sign in with
OneID" and lands in the previous citizen's cabinet with no password.

It lives on `sessions` rather than on `users` because that is its real
lifetime — one browser session, cleared by `auth.service.logout_session` as it
revokes the row. It is deliberately NOT part of `users.oneid_profile`: that
snapshot is read back for the director_registry basis and partly returned to
the browser, and a bearer token for a state system may not travel inside it.

Nullable with no default: every session that exists today was opened before
this column, and a password or E-IMZO session never has one at all.

Revision ID: 0041
Revises: 0040

Numbered 0041, not 0038: three parallel branches (7.6, 7.7 and the Eskiz text
fix) had already taken 0038, 0039 and 0040 off the same `dev` commit. It was
written on an 0037 parent and re-pointed at 0040 when that branch merged into
`dev` — legitimate only because this revision had run nowhere but one local
test database at the time. A revision that has run anywhere real cannot be
re-pointed; that case takes `alembic merge heads` instead.
Create Date: 2026-09-07 17:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("oneid_access_token", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "oneid_access_token")
