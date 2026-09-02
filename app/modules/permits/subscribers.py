"""What this module does when another module publishes a fact it cares about.

Handlers run synchronously, inside the PUBLISHER's own session and transaction
(`app/core/events.py`): whatever happens here is committed with the payment that
caused it, or rolled back with it. So a handler does the smallest thing that must
not half-happen, and nothing that could fail for its own reasons.

Registration is by NAME, in `app/event_subscriptions.py` — `permits` and
`payments` are both level 4 and neither may import the other (`design/01` rule 3),
which is the whole reason the bus exists.
"""

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event
from app.modules.applications import service as applications_service
from app.modules.notifications import service as notifications
from app.modules.permits import events

logger = structlog.get_logger()


async def on_payment_confirmed(db: AsyncSession, event: Event) -> None:
    """Money has arrived: tell the assigned executor that a permit is now due.

    **It only notifies (ruling 19).** `design/03` makes issuance a human act, and a
    document carrying a series number must not appear because a webhook fired.
    `tz/04` С10 ends «Далее — уведомление и запуск формирования разрешения», and
    the notification is the half a machine may do.

    The event carries `application_id` and NOTHING else (`applications/events.py`'s
    payload contract, which 3.10a's `payment_confirmed` follows). Everything else —
    the number, and above all the amount — is read back through the public surface:
    a figure carried on an event would be a SECOND source of truth for money
    alongside the stored calculation.

    `assigned_user_id` is null until an executor picks the application up. There is
    then nobody to tell, and inventing a recipient (the whole leshoz, say) would be
    worse than saying nothing — so this logs and returns. Nothing here raises on a
    missing row either: this handler runs inside the payment's transaction, and a
    permit-side notification failure must never roll back a confirmed payment.
    """
    application_id = event.payload.get("application_id")
    if application_id is None:
        logger.warning("permits.payment_confirmed_without_application", event=event.name)
        return

    application = await applications_service.get(db, application_id)
    if application is None:
        logger.warning("permits.payment_confirmed_unknown_application", id=str(application_id))
        return
    if application.assigned_user_id is None:
        logger.info("permits.payment_confirmed_unassigned", id=str(application_id))
        return

    calculation = await applications_service.current_calculation(db, application_id)
    await notifications.notify(
        db,
        event_code=events.PAYMENT_CONFIRMED,
        recipient_user_id=application.assigned_user_id,
        params={
            "application_number": application.number or str(application.id),
            "amount": calculation.amount if calculation is not None else "",
        },
        object_type="application",
        object_id=application.id,
    )
