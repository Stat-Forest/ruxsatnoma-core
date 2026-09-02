"""Queries and writes for `applications`. Nothing here decides anything —
transition legality, permission and zone rules all belong to `service.py`;
repo only reads and writes rows (design/01 rule 2: router -> service -> repo
-> models). Branch 1 (`stage-3.9a-core`) needs exactly the two functions
below, for `service.get` and `service.set_status`; branch 2 adds the rest
(the duplicate-guard read, submission writes, precheck/decision queries)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications.models import Application, ApplicationStatusHistory


async def get_application(db: AsyncSession, application_id: uuid.UUID) -> Application | None:
    return await db.get(Application, application_id)


async def add_status_history(db: AsyncSession, entry: ApplicationStatusHistory) -> None:
    """Stage the entry and flush — together with whatever else is dirty on
    the session (`set_status`'s own `applications.status` UPDATE), so the
    append-only trigger and the two status CHECK constraints surface at the
    call site. Mirrors `audit.repo.add`."""
    db.add(entry)
    await db.flush()
