"""Who still holds unfinished work — asked by `admin`, answered by modules above it.

`tz/04` С23 makes deletion conditional on a handover ("удаление — только после
передачи незавершённых дел другому исполнителю"), and its adjacent line makes
archival of an organization conditional on the same kind of question (finding
F5 of `docs/plans/07.5-audit-findings.md`). The modules that know what a USER
still holds are `applications` (level 3) and `inspections` (level 5), both
above `admin` (level 1); the modules that know what hangs off an ORGANIZATION
are the same ones. A module may call its own level and downwards only
(design/01 rule 3), so the question travels the way this codebase already
sends questions upwards: a registry of providers, filled in
`app/event_subscriptions.py` — the same idiom as `gis.OCCUPANCY_PROVIDERS`,
`norms.LOAD_PROVIDERS` and `core.files.ACCESS_CHECKS`.

This module imports no domain module, deliberately: that is what keeps the
direction of dependency pointing the right way — a module above `admin` may
import THIS module to register, and `admin` itself never imports any of them.

Two registries, not one, because the two questions ("what does this USER
hold" and "what does this ORGANIZATION hold") have different keys and
different answers (an application assigned to a user is one row; an
application merely routed through an organization via `assigned_org_id` says
nothing about which of its staff is on the hook) — but the same shape serves
both, so `OpenWork` and the fail-closed aggregation rule are written once and
reused.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class OpenWork:
    """One kind of unfinished work, with enough detail for an admin to act.

    A refusal that does not name what is held is the same hiding-not-leaking
    shape every audit in this project has found (F2 of 07.5, C16's silent
    violator) — `as_details()` is what a caller puts in `DomainError.details`
    so the admin sees exactly what to hand over. `ids` is capped by its
    PROVIDER, not here: the refusal is a message to a person, and a thousand
    ids in an error body helps nobody.
    """

    kind: str
    count: int
    ids: list[uuid.UUID] = field(default_factory=list)

    def as_details(self) -> dict[str, object]:
        return {"kind": self.kind, "count": self.count, "ids": [str(i) for i in self.ids]}


# --- Task 1: what a USER still holds (`delete_user`'s guard, F4) ------------

OpenWorkProvider = Callable[[AsyncSession, uuid.UUID], Awaitable[OpenWork | None]]
OPEN_WORK_PROVIDERS: list[OpenWorkProvider] = []


async def open_work_for(db: AsyncSession, user_id: uuid.UUID) -> list[OpenWork]:
    """Every registered provider's answer for `user_id`, empty entries dropped.

    Deliberately NOT exception-tolerant: a provider that raises leaves the
    question unanswered, and answering "nothing held" to an unanswered
    question is the defect this exists to close (F4). Let it propagate — the
    caller's transaction rolls back and the user is not deleted, which is the
    fail-closed direction this whole seam exists for.
    """
    held: list[OpenWork] = []
    for provider in OPEN_WORK_PROVIDERS:
        answer = await provider(db, user_id)
        if answer is not None and answer.count:
            held.append(answer)
    return held


# --- Task 3: what an ORGANIZATION still has hanging off it (`archive_organization`, F5) ---

OrgWorkProvider = Callable[[AsyncSession, uuid.UUID], Awaitable[OpenWork | None]]
ORG_WORK_PROVIDERS: list[OrgWorkProvider] = []


async def org_work_for(db: AsyncSession, organization_id: uuid.UUID) -> list[OpenWork]:
    """Every registered provider's answer for `organization_id` — the same
    fail-closed rule as `open_work_for`, for the same reason: an organization
    archived while a provider cannot answer would be exactly as wrong as a
    user deleted the same way."""
    held: list[OpenWork] = []
    for provider in ORG_WORK_PROVIDERS:
        answer = await provider(db, organization_id)
        if answer is not None and answer.count:
            held.append(answer)
    return held
