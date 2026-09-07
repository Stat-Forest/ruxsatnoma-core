"""Who still holds unfinished work — asked by `admin`, answered by modules above it.

`tz/04` С23 makes deletion conditional on a handover, and the modules that know
what a user still holds are `applications` (level 3) and `inspections` (level 5),
both above `admin` (level 1). A module may call its own level and downwards only
(design/01 rule 3), so the question travels the way this codebase already sends
questions upwards: a registry of providers, filled in `app/event_subscriptions.py`
— the same idiom as `gis.OCCUPANCY_PROVIDERS`, `norms.LOAD_PROVIDERS` and
`core.files.ACCESS_CHECKS`.

This module imports no domain module, deliberately: that is what keeps the
direction of dependency pointing the right way.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class OpenWork:
    """One kind of unfinished work, with enough detail for an admin to act.

    `ids` is capped by its provider, not here: the refusal is a message to a
    person, and a thousand ids in an error body helps nobody.
    """

    kind: str
    count: int
    ids: list[uuid.UUID] = field(default_factory=list)

    def as_details(self) -> dict[str, object]:
        return {"kind": self.kind, "count": self.count, "ids": [str(i) for i in self.ids]}


OpenWorkProvider = Callable[[AsyncSession, uuid.UUID], Awaitable[OpenWork | None]]
OPEN_WORK_PROVIDERS: list[OpenWorkProvider] = []


async def open_work_for(db: AsyncSession, user_id: uuid.UUID) -> list[OpenWork]:
    """Every provider's answer, empty entries dropped.

    Deliberately NOT exception-tolerant: a provider that raises leaves the
    question unanswered, and answering "nothing held" to an unanswered question
    is the defect this exists to close (F4). Let it propagate.
    """
    held = []
    for provider in OPEN_WORK_PROVIDERS:
        answer = await provider(db, user_id)
        if answer is not None and answer.count:
            held.append(answer)
    return held
