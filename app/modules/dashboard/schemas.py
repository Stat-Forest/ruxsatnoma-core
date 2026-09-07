"""Output schemas for the dashboard read surface (С21). No input schema:
every route is a filtered read (design/01 rule 5)."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class PeriodOut(BaseModel):
    period_from: date
    period_to: date


class PermitsKpiOut(BaseModel):
    issued_count: int
    active_count: int
    previous_issued_count: int | None = None


class ApplicationsKpiOut(BaseModel):
    total_count: int
    by_status: dict[str, int]
    previous_total_count: int | None = None


class OccupancyKpiOut(BaseModel):
    contour_count: int
    avg_occupied_pct: Decimal | None


class PaymentsKpiOut(BaseModel):
    invoiced_amount: Decimal
    paid_amount: Decimal
    budget_share_amount: Decimal
    recipient_share_amount: Decimal


class SlaKpiOut(BaseModel):
    active_count: int
    overdue_count: int


class RejectionRowOut(BaseModel):
    reason_item_id: uuid.UUID
    count: int


class RiskIndicatorsKpiOut(BaseModel):
    by_code: dict[str, int]
    by_level: dict[str, int]


class InspectionsKpiOut(BaseModel):
    inspections_count: int
    violations_count: int


class SatisfactionKpiOut(BaseModel):
    """Ruling #143. `avg_score` is `None`, never `0`, for a period with no
    ratings — a portal may not state a number it cannot produce."""

    avg_score: Decimal | None
    count: int


class KpiOut(BaseModel):
    period: PeriodOut
    permits: PermitsKpiOut
    applications: ApplicationsKpiOut
    occupancy: OccupancyKpiOut
    sb_load_total: Decimal
    payments: PaymentsKpiOut
    sla: SlaKpiOut
    rejections: list[RejectionRowOut]
    risk_indicators: RiskIndicatorsKpiOut
    inspections: InspectionsKpiOut
    satisfaction: SatisfactionKpiOut
    # Tiles `tz/04` С21 names that this module cannot yet build for lack of a
    # real source (track brief's own rule: no plausible constant, leave it
    # out and say so instead). Empty today — `inspections`/`violations` were
    # the two names here until `repo.inspections_kpi` gave them a real source
    # (seam audit, 2026-09-06).
    omitted: list[str]


class SliceCellOut(BaseModel):
    level: str
    key: uuid.UUID | None
    label: str
    applications_count: int | None
    permits_count: int | None
    # `None` whenever `suppressed` is true: the whole point of k-anonymity is
    # not showing a small exact number, so the count that TRIGGERED
    # suppression must not leak in the very response that hides everything
    # else.
    applicant_count: int | None
    suppressed: bool


class TerritorySliceOut(BaseModel):
    level: str
    k_anonymity_threshold: int
    cells: list[SliceCellOut]
