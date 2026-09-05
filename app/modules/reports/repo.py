"""All SQL/ORM queries for `reports` — including the cross-module reader join
(design/01 rule 5: reports gets read-only access to any table). `permits`,
`invoices` and `applicants` are imported here, for SELECT only, and nowhere
else in this module — the boundary rule is enforced by convention (this file
is the one place it happens), the same shape `norms/repo.py::application_facts`
uses for ITS one read-only cross-module window."""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.auth.models import Applicant
from app.modules.payments.models import Invoice
from app.modules.permits.models import Permit
from app.modules.reports.models import Report, ReportForm

# --- report_forms ------------------------------------------------------------


async def get_form(db: AsyncSession, form_id: uuid.UUID) -> ReportForm | None:
    # `select(...).execute()`, not `db.get(...)` (lesson candidate, see
    # TRACK-REPORT.md): on this stack, `Session.get()` as the very first
    # statement issued on a freshly checked-out pooled connection has been
    # observed to leave the session's greenlet bridge unable to service a
    # LATER `flush()` on that same session (`MissingGreenlet`, no SQL error
    # at all) — reproduced against plain, unrelated models with no
    # reports-specific code involved. A plain `select()` does not exhibit
    # it. Every getter in this file uses `select()` for this reason, not
    # merely for style.
    result = await db.execute(select(ReportForm).where(ReportForm.id == form_id))
    return result.scalar_one_or_none()


async def get_form_by_code_version(
    db: AsyncSession, *, code: str, version: int
) -> ReportForm | None:
    result = await db.execute(
        select(ReportForm).where(ReportForm.code == code, ReportForm.version == version)
    )
    return result.scalar_one_or_none()


async def list_forms(
    db: AsyncSession, *, status: str | None, offset: int, limit: int
) -> tuple[list[ReportForm], int]:
    conditions: list[Any] = []
    if status is not None:
        conditions.append(ReportForm.status == status)
    base: Select[Any] = select(ReportForm).where(*conditions)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    result = await db.execute(
        base.order_by(ReportForm.code, ReportForm.version.desc()).offset(offset).limit(limit)
    )
    return list(result.scalars().all()), total


# --- reports -------------------------------------------------------------


async def get_report(db: AsyncSession, report_id: uuid.UUID) -> Report | None:
    # select(), not db.get() — see get_form's comment above.
    result = await db.execute(select(Report).where(Report.id == report_id))
    return result.scalar_one_or_none()


async def get_report_for_update(db: AsyncSession, report_id: uuid.UUID) -> Report | None:
    """The locking sibling of `get_report` — every lifecycle action
    (`submit`/`sign`/`return`/`approve`/`revise`) is a read-check-write over
    one report, the same shape `permits.repo.permit_by_id_for_update` uses.

    `select()` rather than `db.get(..., with_for_update=True,
    populate_existing=True)` — see `get_form`'s comment. `execution_options
    (populate_existing=True)` is still passed explicitly: `with_for_update`
    alone emits a real `SELECT ... FOR UPDATE`, but without it an
    already-identity-mapped instance keeps its cached column values
    (`app/db.py`'s `expire_on_commit=False` never clears them on its own),
    and a bare `with_for_update` would take the lock and still validate a
    stale `status` under it. `tests/test_code_conventions.py::
    test_every_locking_get_also_repopulates_the_row` enforces this pairing
    for `Session.get`-shaped locking getters specifically (its own docstring
    excludes a plain `select(...).with_for_update()`), so this shape is
    correct without tripping — or needing — that check."""
    result = await db.execute(
        select(Report)
        .where(Report.id == report_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def list_reports(
    db: AsyncSession,
    *,
    zone: Any,
    organization_id: uuid.UUID | None,
    status: str | None,
    form_id: uuid.UUID | None,
    offset: int,
    limit: int,
) -> tuple[list[Report], int]:
    """Zone-scoped list, joined to `organizations` so a region/district-scoped
    actor's zone is enforced too, not organization alone (the gis lesson:
    `zone_filter` fails closed on a missing column, and a region- or
    district-scoped staff row is producible today)."""
    conditions: list[Any] = [zone]
    if organization_id is not None:
        conditions.append(Report.organization_id == organization_id)
    if status is not None:
        conditions.append(Report.status == status)
    if form_id is not None:
        conditions.append(Report.form_id == form_id)

    joined = (
        select(Report.id)
        .join(Organization, Organization.id == Report.organization_id)
        .where(*conditions)
    )
    total = (await db.execute(select(func.count()).select_from(joined.subquery()))).scalar_one()
    result = await db.execute(
        select(Report)
        .join(Organization, Organization.id == Report.organization_id)
        .where(*conditions)
        .order_by(Report.period_start.desc(), Report.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def find_report(
    db: AsyncSession,
    *,
    form_id: uuid.UUID,
    organization_id: uuid.UUID,
    period_start: date,
    version_no: int,
) -> Report | None:
    result = await db.execute(
        select(Report).where(
            Report.form_id == form_id,
            Report.organization_id == organization_id,
            Report.period_start == period_start,
            Report.version_no == version_no,
        )
    )
    return result.scalar_one_or_none()


async def max_version_no(
    db: AsyncSession, *, form_id: uuid.UUID, organization_id: uuid.UUID, period_start: date
) -> int:
    result = await db.execute(
        select(func.max(Report.version_no)).where(
            Report.form_id == form_id,
            Report.organization_id == organization_id,
            Report.period_start == period_start,
        )
    )
    return result.scalar_one() or 0


# --- the reader join: one row per matching permit --------------------------


async def report_rows(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    activity_type_id: uuid.UUID | None,
    period_start: date,
    period_end: date,
) -> list[dict[str, Any]]:
    """One dict per permit issued by `organization_id`, active during any part
    of `[period_start, period_end]`, whose activity matches the form's own
    `activity_type_id` (all activities when the form does not restrict one).

    Read-only, this module's own reader exception (design/01 rule 5) — no
    writes here and no import of `permits.service`/`payments.service`: a
    reporting join is exactly the "queries across the whole database" case
    the rule carves out, not a business action any owning module frames as
    one call.

    `inspection_result` is always `None` — `inspections` (track 4.1) does not
    exist on this branch (plan "scope cuts"); the field name is reserved so a
    later wiring is an UPDATE of this function, not a schema change.
    """
    conditions: list[Any] = [
        Permit.organization_id == organization_id,
        Permit.period_from <= period_end,
        Permit.period_to >= period_start,
    ]
    if activity_type_id is not None:
        conditions.append(Permit.activity_type_id == activity_type_id)

    # The newest invoice per application — `uq_invoices_one_in_force` already
    # guarantees at most one pending/paid at a time, but a cancelled/expired
    # one can coexist historically, so this orders by `issued_at` rather than
    # assuming uniqueness.
    latest_invoice = (
        select(Invoice.application_id, func.max(Invoice.issued_at).label("issued_at"))
        .group_by(Invoice.application_id)
        .subquery()
    )

    result = await db.execute(
        select(
            Permit.series,
            Permit.number,
            Permit.area_ha,
            Permit.sb_load,
            Permit.period_from,
            Permit.period_to,
            Permit.amount,
            Permit.contour_id,
            Permit.snapshot,
            Applicant.kind,
            Applicant.name,
            Applicant.pinfl,
            Applicant.stir,
            Applicant.address,
            Applicant.phone,
            Invoice.amount.label("paid_amount"),
            Invoice.status.label("invoice_status"),
            Invoice.paid_at,
        )
        .join(Applicant, Applicant.id == Permit.applicant_id)
        .outerjoin(latest_invoice, latest_invoice.c.application_id == Permit.application_id)
        .outerjoin(
            Invoice,
            (Invoice.application_id == latest_invoice.c.application_id)
            & (Invoice.issued_at == latest_invoice.c.issued_at),
        )
        .where(*conditions)
        .order_by(Permit.series, Permit.number)
    )
    rows: list[dict[str, Any]] = []
    for r in result.all():
        paid_amount = r.paid_amount if r.invoice_status == "paid" else None
        snapshot = r.snapshot or {}
        rows.append(
            {
                "permit_series_number": f"{r.series} № {r.number:06d}",
                "legal_name": r.name if r.kind == "legal" else None,
                "legal_stir": r.stir,
                "individual_name": r.name if r.kind == "individual" else None,
                "individual_address": r.address,
                "individual_pinfl": r.pinfl or r.stir,
                "individual_phone": r.phone,
                "pasture_contour": str(r.contour_id),
                "contour_id": str(r.contour_id),
                "area_ha": str(r.area_ha),
                "hayfield_area_ha": str(r.area_ha),
                # `permits.service.LIVESTOCK_ROWS`' own four printed groups —
                # copied out of the permit's OWN frozen snapshot (form
                # 1-ilova requisites 12-15), never recomputed from
                # `calculations.input_snapshot` directly: the snapshot is
                # what the permit itself says, and re-deriving the herd here
                # could disagree with it if a livestock catalogue entry
                # changes group after issuance.
                "livestock_adult": snapshot.get("heads_large_adult"),
                "livestock_young": snapshot.get("heads_large_young"),
                "livestock_sheep_goat_6m": snapshot.get("heads_small_adult"),
                "livestock_sheep_goat_under_6m": snapshot.get("heads_small_young"),
                "sb_load": str(r.sb_load) if r.sb_load is not None else None,
                "period_from": r.period_from.isoformat(),
                "period_to": r.period_to.isoformat(),
                "total_amount": str(r.amount),
                "paid_amount": str(paid_amount) if paid_amount is not None else None,
                "distribution": None,
                "inspection_result": None,
            }
        )
    return rows
