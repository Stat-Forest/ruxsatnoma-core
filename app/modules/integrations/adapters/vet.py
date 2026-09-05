"""Veterinary registry check seam (stage 3.9b, tz/09 row 6: "Veterinary AT —
livestock counts, veterinary status — synchronous API + fallback document",
medium priority, no verified contract).

Same Protocol + mock + factory shape as every other adapter in this package
(`oneid.py`, `otp_sender.py`) — except the `real` branch: `design/04` covers
only the four v1 systems, and a real adapter written against a contract
nobody has read would be a worse defect than having none at all
(`test_the_real_adapter_refuses_until_a_contract_exists`). It stays this way
until the Agency's veterinary AT contract is actually verified.

**`get_adapter()` also refuses the MOCK in `app_env=prod`** (fix round 1,
2026-09-05 controller ruling — the 2026-08-28 stage 3.2b entry, ruling 2:
"prod refuses to start with any mock"). `vet_mode` stays OUT of
`Settings._forbid_default_secret_in_prod`'s mocked-adapter list on purpose —
prod may still START with no veterinary contract, since nothing schedules
one — but it may not ANSWER a check from a fixture: `MockVetAdapter.check()`
always returns `result="pass"`, and on the wire that is indistinguishable
from a genuine registry answer, with only a buried `details.note` telling
them apart. A leshoz head deciding on that verdict would be trusting nobody.
So in production every vet check goes through the paper fallback instead
(`applications.service.add_check`'s `source="manual_fallback"` branch, under
maker-checker) — the honest path, since it already exists.
"""

import uuid
from dataclasses import dataclass
from typing import Protocol

from app.config import get_settings


@dataclass(frozen=True)
class VetCheckResult:
    """`result` is one of `application_checks.CHECK_RESULTS` minus
    `skipped` — that fourth value is `gis`/`norms`' own designed branch for an
    empty reference layer (models.py); an unreachable veterinary registry is
    the manual-fallback path (`applications.service.add_check`), not a fourth
    verdict here."""

    result: str
    details: dict[str, str]


class VetAdapter(Protocol):
    async def check(self, *, application_id: uuid.UUID) -> VetCheckResult: ...


class MockVetAdapter:
    """No live registry to query: a fixed `pass` lets the review flow
    (`POST /applications/{id}/checks`) be exercised end to end without one,
    the same reason `MockOneId`/`MockOtpSender` beside it answer
    unconditionally rather than reject."""

    async def check(self, *, application_id: uuid.UUID) -> VetCheckResult:
        return VetCheckResult(
            result="pass",
            details={"note": "mock veterinary adapter — no live registry configured"},
        )


def get_adapter() -> VetAdapter:
    settings = get_settings()
    if settings.vet_mode == "real":
        raise NotImplementedError(
            "the veterinary AT has no verified contract (tz/09 row 6, medium priority) — "
            "a real adapter is written against one, never guessed"
        )
    if settings.app_env == "prod":
        raise NotImplementedError(
            "a mock veterinary adapter may not answer a live check in app_env=prod — "
            "no verified contract exists to make it real, so a production check goes "
            "through the paper fallback (source=manual_fallback) under maker-checker "
            "instead of a fixture's fixed verdict"
        )
    return MockVetAdapter()
