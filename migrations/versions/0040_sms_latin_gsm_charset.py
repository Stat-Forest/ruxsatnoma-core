"""sms_latin_gsm_charset

Normalise the Latin-Uzbek SMS bodies to the GSM 03.38 character set: `ʻ`
(U+02BB) and `ʼ` (U+02BC) become the plain ASCII apostrophe, and the em/en
dashes become a hyphen. `uz_cyrl` and `ru` are deliberately untouched, and so
is every `inapp` and `email` row — this is a cost fix on one channel, not an
orthography change.

**Why.** An operator bills one SMS per 160 characters only while every
character is in GSM 03.38; a single character outside it switches the whole
message to UCS-2 and 70 characters per part. `oʻ`/`gʻ` are correct Uzbek
orthography and are outside that set, so six of the twenty-five Latin SMS
bodies were billing two parts for a sentence of 73-88 characters —
`application.sla_approaching`, `invoice.due_soon`, `invoice.issued`,
`payment.manual_confirmed`, `permit.active` and `permit.issued`. All six fit
in one part after this migration; the whole set drops from 34 billed parts to
28. `o'` is how Uzbek is written in SMS anyway, and the cabinet keeps the
correct spelling because its `inapp` rows are separate rows.

The texts have to be right BEFORE they go to Eskiz: every SMS body is
moderated by the provider before it can be sent, an unapproved text simply
does not arrive, and changing one afterwards means moderating it again
(decision #48 ruling 2; `design/04-integrations.md` §4).

**A targeted UPDATE, not a re-seed and not a new version** — the same call
`0025` made for `classifier_items.props`. Nothing about the sentence changes,
so archiving the row and inserting a v2 would put a version bump in an admin's
history that says nothing happened. Archived rows are left alone on purpose:
they record what was actually sent.

`0038` and `0039` were taken by the parallel 7.6 and 7.7 branches, which is why this
was authored as `0040` on a `0037` parent. At integration the two heads (`0039` and
this branch's `0042`) were reconciled by re-pointing `0039`'s `down_revision` at
`0042` rather than by `alembic merge heads` — `0039` had already been applied to a
long-lived local database under its old parent, and a merge revision would have
renumbered nothing while a plain renumber of `0039` would have needed a hand-edit
of `alembic_version` on every machine that ran it. That repoint only works if this
migration sits between `0038` and `0039` rather than beside them, so `0040`'s own
`down_revision` was moved from `0037` to `0038` in the same integration commit —
the chain is not monotonic (see `../CLAUDE.md`), and the highest number is never
the head.

`tests/modules/notifications/test_sms_gsm_charset.py` is what keeps the next
seeded SMS template inside the set.

Revision ID: 0040
Revises: 0038
Create Date: 2026-09-07 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0040"
down_revision: str | Sequence[str] | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Repeated as literals rather than imported from `app`: a migration is a frozen
# historical statement (0009/0020/0025/0036 all make the same call).
_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("ʻ", "'"),  # ʻ — the letter oʻ/gʻ modifier, by far the common one
    ("ʼ", "'"),  # ʼ — the tutuq belgisi
    ("‘", "'"),  # ‘ and ’ — typographic quotes, wrong but cheap to cover
    ("’", "'"),
    ("—", "-"),  # — em dash, used in the two date-range bodies
    ("–", "-"),  # – en dash
)


def _quote(value: str) -> str:
    """A SQL string literal. The apostrophe is the whole point of this migration,
    so doubling it is not a formality here — an unescaped one ends the literal."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _expression(column: str) -> str:
    """Nested `replace()` over one JSONB text value, innermost first."""
    expr = column
    for source, target in _REPLACEMENTS:
        expr = f"replace({expr}, {_quote(source)}, {_quote(target)})"
    return expr


def upgrade() -> None:
    """Upgrade schema."""
    normalised = _expression("body->>'uz_latn'")
    op.execute(
        sa.text(
            "UPDATE notification_templates"
            f" SET body = jsonb_set(body, '{{uz_latn}}', to_jsonb({normalised}))"
            " WHERE channel = 'sms'"
            "   AND status = 'active'"
            "   AND jsonb_exists(body, 'uz_latn')"
            f"   AND body->>'uz_latn' <> {normalised}"
        )
    )


def downgrade() -> None:
    """Downgrade schema.

    A no-op, deliberately. `'` cannot be mapped back: the apostrophe this
    migration writes is indistinguishable from one an author typed, so any
    reverse replacement would corrupt bodies it never touched — the same
    honesty `0032`'s downgrade documents, one step further (it could at least
    re-derive its own output; here the input is unrecoverable). Re-running the
    upgrade is idempotent, so a downgrade/upgrade round trip is safe.
    """
