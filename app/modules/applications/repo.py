"""Queries and writes for `applications`. Nothing here decides anything —
transition legality, permission and zone rules all belong to `service.py`;
repo only reads and writes rows (design/01 rule 2: router -> service -> repo
-> models). Branch 1 (`stage-3.9a-core`) needs exactly the three functions
below, for `service.get` and `service.set_status`; branch 2 adds the rest
(the duplicate-guard read, submission writes, precheck/decision queries)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications.models import Application, ApplicationStatusHistory


async def get_application(db: AsyncSession, application_id: uuid.UUID) -> Application | None:
    return await db.get(Application, application_id)


async def get_application_for_update(
    db: AsyncSession, application_id: uuid.UUID
) -> Application | None:
    """`service.get`'s locking sibling — `set_status` ONLY (review C1).
    `SELECT ... FOR UPDATE` so two concurrent transitions on the same
    application serialise instead of racing: without it, two callers who
    both read the same pre-write status (a scheduler job and an HTTP
    callback, say) can both pass `_assert_transition` and both write, and
    the loser's UPDATE silently overwrites the winner's — exactly the bug
    `notifications.service._deliver`'s own `with_for_update` already exists
    to prevent for the identical read-check-write shape. The second caller
    here blocks until the first commits or rolls back, then re-reads the
    now-current status, so a genuine conflict surfaces as `ERR-APP-004`
    instead of a lost write."""
    return await db.get(Application, application_id, with_for_update=True)


async def add_status_history(db: AsyncSession, entry: ApplicationStatusHistory) -> None:
    """Stage the entry and flush — together with whatever else is dirty on
    the session (`set_status`'s own `applications.status` UPDATE), so the
    append-only trigger and the two status CHECK constraints surface at the
    call site. Mirrors `audit.repo.add`."""
    db.add(entry)
    await db.flush()
