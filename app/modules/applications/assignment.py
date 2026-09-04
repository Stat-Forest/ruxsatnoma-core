"""Auto-assignment on submission (plan 03.9b task 1; design/03 § Заявки).

`choose_executor` is the only thing in this file, and it is PURE — no I/O, no
database, not even a reference to `Application` — so ruling 7's tie-break rule
can be tested without a database. `Candidate` is the shape
`applications.repo.review_candidates` answers with: one user ELIGIBLE to
review (auth's own question — who holds `applications.review` in the
organization) and how many applications they currently hold (this module's
own `application_assignments` table, still `is_active`). Keeping the CHOICE
here and the LOOKUP in the repo is what lets the tie-break and the empty case
be tested without either database or the auth module.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Candidate:
    """One reviewer eligible for auto-assignment, and their current workload."""

    user_id: uuid.UUID
    open_count: int


def choose_executor(candidates: Sequence[Candidate]) -> uuid.UUID | None:
    """The least-loaded candidate, ties broken by `user_id` so the pick is
    deterministic (ruling 7: a non-deterministic assignment is untestable and
    produces support tickets nobody can reproduce).

    `None` when there is nobody eligible — the application is still assigned
    to the ORGANIZATION (`service._effective_organization` sets that
    regardless of this function's answer); auto-assignment must never refuse
    or delay a submission for lack of a reviewer to name.
    """
    if not candidates:
        return None
    return min(candidates, key=lambda c: (c.open_count, c.user_id)).user_id
