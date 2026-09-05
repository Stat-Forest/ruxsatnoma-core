"""Cadastre / GIS-source check seam (stage 3.9b, tz/09 row 5: "Cadastre / GIS
sources — contours, legal status — API / WFS / periodic sync", medium
priority, no verified contract).

Same Protocol + mock + factory shape as `vet.py` beside it, and the same
reason for its `real` branch: `design/04` covers only the four v1 systems, so
a real adapter here would be written against a contract nobody has read
(`test_the_real_adapter_refuses_until_a_contract_exists`, `vet.py`'s own
copy). Not to be confused with `gis` (this repository's own contour module,
already real) — this is the EXTERNAL state cadastre `tz/09` lists separately.

**`get_adapter()` also refuses the MOCK in `app_env=prod`** — `vet.py`'s own
copy of the same fix-round-1 rule (2026-09-05 controller ruling, the
2026-08-28 stage 3.2b entry ruling 2). `cadastre_mode` stays out of
`Settings._forbid_default_secret_in_prod`'s mocked-adapter list so prod may
still START with no cadastre contract, but it may not ANSWER a check from a
fixture — see `vet.py`'s docstring for the full reasoning, identical here.
"""

import uuid
from dataclasses import dataclass
from typing import Protocol

from app.config import get_settings


@dataclass(frozen=True)
class CadastreCheckResult:
    """`result` is one of `application_checks.CHECK_RESULTS` minus
    `skipped` — see `vet.VetCheckResult`'s own docstring for why."""

    result: str
    details: dict[str, str]


class CadastreAdapter(Protocol):
    async def check(self, *, application_id: uuid.UUID) -> CadastreCheckResult: ...


class MockCadastreAdapter:
    """No live registry to query: a fixed `pass`, `vet.MockVetAdapter`'s own
    reasoning."""

    async def check(self, *, application_id: uuid.UUID) -> CadastreCheckResult:
        return CadastreCheckResult(
            result="pass",
            details={"note": "mock cadastre adapter — no live registry configured"},
        )


def get_adapter() -> CadastreAdapter:
    settings = get_settings()
    if settings.cadastre_mode == "real":
        raise NotImplementedError(
            "the cadastre/GIS source has no verified contract (tz/09 row 5, medium priority) — "
            "a real adapter is written against one, never guessed"
        )
    if settings.app_env == "prod":
        raise NotImplementedError(
            "a mock cadastre adapter may not answer a live check in app_env=prod — "
            "no verified contract exists to make it real, so a production check goes "
            "through the paper fallback (source=manual_fallback) under maker-checker "
            "instead of a fixture's fixed verdict"
        )
    return MockCadastreAdapter()
