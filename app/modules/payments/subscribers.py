"""Event-bus handlers for `payments` — registered by
`app.event_subscriptions.register_event_subscriptions()` and nowhere else
(design/01 rule 4). Never called directly and never subscribed anywhere else.

Each handler takes `application_id` off the payload and NOTHING else
(`applications.events`'s own payload contract) — it must not assume any
other key is present, and must tolerate extra keys appearing later. Every
other fact about the application is read through `applications.service`'s
public surface, inside `payments.service`, never off the event itself.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event
from app.modules.payments import service


def _application_id(event: Event) -> uuid.UUID:
    """Normalizes `application_id` to a `uuid.UUID` regardless of whether the
    publisher handed it over as one already or as its string form (both
    shapes occur: a same-process publisher may pass the ORM column's own
    `uuid.UUID` value, a test may serialize it to `str` first)."""
    return uuid.UUID(str(event.payload["application_id"]))


async def on_application_approved(db: AsyncSession, event: Event) -> None:
    """`applications.events.APPLICATION_APPROVED` -> issue the invoice."""
    await service.issue_invoice(db, _application_id(event))


async def on_application_cancelled(db: AsyncSession, event: Event) -> None:
    """`applications.events.APPLICATION_CANCELLED` -> cancel any in-force
    invoice. Idempotent and silent when there is none."""
    await service.cancel_invoice_for_application(db, _application_id(event))
