"""Output schemas for the oversight read surface. No input schema is needed:
every route is a filtered list (design/01 rule 5 — a reader writes nothing of
its own through the API)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.modules.oversight.models import (
    RISK_INDICATOR_CODES,
    RISK_INDICATOR_LEVELS,
    RISK_INDICATOR_STATUSES,
)

# Spelled out by hand rather than `Literal[*RISK_INDICATOR_CODES]` (pyright
# rejects a variable in a type expression — the same reason `norms/schemas.py`
# gives for its own literals); `test_models.py` pins the two equal.
RiskIndicatorCode = Literal[
    "RI-01",
    "RI-02",
    "RI-03",
    "RI-04",
    "RI-05",
    "RI-06",
    "RI-07",
    "RI-08",
    "RI-09",
    "RI-10",
    "RI-11",
    "RI-12",
    "RI-13",
    "RI-14",
    "RI-15",
]
assert set(RiskIndicatorCode.__args__) == set(RISK_INDICATOR_CODES)  # type: ignore[attr-defined]

RiskIndicatorLevel = Literal["low", "medium", "high", "critical"]
assert set(RiskIndicatorLevel.__args__) == set(RISK_INDICATOR_LEVELS)  # type: ignore[attr-defined]

RiskIndicatorStatus = Literal["new", "in_review", "closed"]
assert set(RiskIndicatorStatus.__args__) == set(RISK_INDICATOR_STATUSES)  # type: ignore[attr-defined]


class RiskIndicatorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    level: str
    object_type: str | None
    object_id: uuid.UUID | None
    responsible_user_id: uuid.UUID | None
    description: str
    details: dict[str, Any] | None
    occurred_at: datetime
    status: str
    rn_status: str


class OversightEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    object_type: str | None
    object_id: uuid.UUID | None
    payload: dict[str, Any] | None
    correlation_id: str | None
    occurred_at: datetime
    rn_status: str
