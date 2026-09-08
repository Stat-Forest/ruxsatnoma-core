"""invoice_issued_says_approved

Fold the approval into the invoice SMS, because the two arrive together.

Approving an application publishes `application_approved` in the same
transaction, `payments`' subscriber issues the invoice and notifies a few
lines later, and both messages reach the phone within seconds of each other.
Decision #152 stops the approval from taking the SMS channel at all
(`applications.decision._notify_decision(..., channels=("inapp",))`), which
leaves this text to carry both pieces of news — so it has to say the first one.

Only the `sms` row is rewritten. The `inapp` copy still arrives beside its own
`application.approved` notification in the cabinet, where saying "approved"
twice would be the wrong text, not the right one.

All three languages are updated even though only `uz_latn` is ever sent
(decision #151): the day a second language is moderated, the text should
already be right rather than a year out of date.

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-08 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0044"
down_revision: str | Sequence[str] | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_CODE = "invoice.issued"

# The apostrophes are plain ASCII on purpose (migration 0040): one character
# outside GSM 03.38 doubles the price of the whole message.
NEW_BODY: dict[str, str] = {
    "uz_latn": "Ariza {application_number} ma'qullandi. To'lov: {amount} so'm, muddat {due_date}.",
    "uz_cyrl": "Ариза {application_number} маъқулланди. Тўлов: {amount} сўм, муддат {due_date}.",
    "ru": "Заявка {application_number} одобрена. К оплате {amount} сум, срок {due_date}.",
}

OLD_BODY: dict[str, str] = {
    "uz_latn": (
        "{application_number} arizasi bo'yicha {amount} so'm to'lov e'lon qilindi."
        " Muddat: {due_date}."
    ),
    "uz_cyrl": (
        "{application_number} аризаси бўйича {amount} сўм тўлов эълон қилинди. Муддат: {due_date}."
    ),
    "ru": "По заявке {application_number} выставлен счёт на {amount} сум. Срок: {due_date}.",
}


def _rewrite(body: dict[str, str]) -> None:
    op.execute(
        sa.text(
            "UPDATE notification_templates SET body = CAST(:body AS jsonb)"
            " WHERE event_code = :code AND channel = 'sms' AND status = 'active'"
        ).bindparams(
            sa.bindparam("body", value=body, type_=postgresql.JSONB()),
            sa.bindparam("code", value=EVENT_CODE),
        )
    )


def upgrade() -> None:
    """Upgrade schema."""
    _rewrite(NEW_BODY)


def downgrade() -> None:
    """Downgrade schema."""
    _rewrite(OLD_BODY)
