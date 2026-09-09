"""Business logic for `reports`: the form catalog and the
Яратилган -> ... -> Тасдиқланган workflow (tz/04 С20).

Level 5 reader (design/01 rule 5): this module's own state lives only in
`report_forms`/`reports`; everything it reads from `permits`/`invoices`/
`applicants` goes through `repo.report_rows`, read-only, never a write.

Plan ruling 1: an APPROVED report is frozen (a database trigger backs this
up — `_assert_editable` is the service-level half, checked BEFORE the
trigger would ever fire). Plan ruling 3: a return re-enters the SAME row
(`returned_by` records who) rather than forking a new one; only a
POST-APPROVAL correction (`revise_report`) creates a new `version_no`.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.abac import Zone, zone_filter, zone_of
from app.core.errors import err
from app.core.schemas import PageParams
from app.modules.admin.models import Organization
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.auth.models import User
from app.modules.reports import render, repo
from app.modules.reports.models import Report, ReportForm
from app.modules.reports.permissions import REPORTS_ACCEPT
from app.modules.reports.rules import check_rows
from app.modules.reports.signers import REPORT_APPROVE_PURPOSE, required_role
from app.modules.signatures import service as signatures_service

OBJECT_TYPE = "report"

# Audit actions: "<object>.<verb>" in English (CLAUDE.md hard rule).
FORM_CREATE = "report_form.create"
FORM_ACTIVATE = "report_form.activate"
FORM_ARCHIVE = "report_form.archive"
REPORT_CREATE = "report.create"
REPORT_GENERATE = "report.generate"
REPORT_UPDATE_DATA = "report.update_data"
REPORT_SUBMIT = "report.submit"
REPORT_SIGN = "report.sign"
REPORT_RETURN = "report.return"
REPORT_APPROVE = "report.approve"
REPORT_REVISE = "report.revise"

# tz/04 С20: a report is mutable while the hodim is still working on it —
# freshly `created`, or sent back for correction (`returned`) — and frozen
# from `submitted` onward until either it comes back `returned` or reaches
# the terminal, database-enforced `approved` freeze.
_EDITABLE_STATUSES = frozenset({"created", "returned"})


# --- zone / identity guards ---------------------------------------------


def _assert_zone(actor: User, organization_id: uuid.UUID) -> None:
    """Write-path zone guard: a zoned actor (a real `organization_id`) may
    only act for their OWN organization; a zone-free actor (central_admin/
    leadership, `organization_id IS NULL`) may act for any — the reading
    that makes `reports.manage`'s central grant (plan ruling 2) meaningful.
    """
    if actor.organization_id is not None and actor.organization_id != organization_id:
        raise err("ERR-ACL-002")


def _in_zone(zone: Zone, org: Organization) -> bool:
    """Per-row equivalent of `zone_filter`'s SQL, for one already-loaded
    organization — the same local copy `permits.service._organization_in_zone`
    and `gis`/`norms` each keep (the module-boundary rule rules out sharing
    it: it is not part of any of their declared public surfaces)."""
    if zone.region_id is not None and zone.region_id != org.region_id:
        return False
    if zone.district_id is not None and zone.district_id != org.district_id:
        return False
    if zone.organization_id is not None and zone.organization_id != org.id:
        return False
    return True


def _assert_editable(report: Report) -> None:
    if report.status not in _EDITABLE_STATUSES:
        raise err("ERR-REP-001", details={"reason": "not_editable", "status": report.status})


async def _assert_report_signer(db: AsyncSession, report: Report, actor: User) -> None:
    """Ruling 4's shape from `permits/signers.py`, one purpose: the rahbar of
    THIS report's own organization, never merely "an executor_head somewhere"
    — `reports.sign` is held by every executor_head nationally, and a
    signature answers "which named official of which organization attests to
    this", not "may this role at all" (the same reasoning `permits` gives for
    refusing `sys_admin` here too — an identity question, not a privilege).

    Early-commit on denial (CLAUDE.md's audit invariant): the raise would
    otherwise roll this audit entry back together with the very exception it
    exists to explain.
    """
    role = await auth_repo.role_code(db, actor)
    wanted = required_role(REPORT_APPROVE_PURPOSE)
    authorized = (
        wanted is not None and role == wanted and actor.organization_id == report.organization_id
    )
    if not authorized:
        await audit.log(
            db,
            action=REPORT_SIGN,
            user_id=actor.id,
            object_type=OBJECT_TYPE,
            object_id=report.id,
            result="denied",
            basis="signer_not_authorized",
        )
        await db.commit()
        raise err("ERR-SIGN-001", details={"reason": "signer_not_authorized"})


async def _assert_may_return_centrally(db: AsyncSession, report: Report, actor: User) -> None:
    """The `head_approved` branch of `return_report` needs `reports.accept`
    itself, not merely something the route's `require_any_permission(
    reports.sign, reports.accept)` gate accepted for the OTHER branch.

    That `any` gate is honest for `submitted` (which legitimately needs
    `reports.sign`, checked by identity in `_assert_report_signer`) and wrong
    for this one: every executor_head nationally holds `reports.sign`, so
    without this check any of them could reach here and return ANY report
    system-wide, recorded as a return "by the centre" they never issued.
    Unlike the signer check, this is a plain PRIVILEGE question — `reports.
    accept` is central-only by construction (migration 0027's seed) and
    carries no per-organization identity of its own — so this mirrors
    `require_permission(REPORTS_ACCEPT)` exactly, `auth.deps._authorize`'s
    sys_admin bypass included (decision #41 ruling 2), rather than
    reimplementing a narrower rule.
    """
    if await auth_repo.role_code(db, actor) == SUPERUSER_ROLE:
        return
    held = await auth_repo.permission_codes(db, actor)
    if REPORTS_ACCEPT not in held:
        await audit.log(
            db,
            action=REPORT_RETURN,
            user_id=actor.id,
            object_type=OBJECT_TYPE,
            object_id=report.id,
            result="denied",
            basis=REPORTS_ACCEPT,
        )
        await db.commit()
        raise err("ERR-ACL-001", details={"permission": REPORTS_ACCEPT})


def _report_bytes(report: Report) -> bytes:
    """The canonical bytes the rahbar's ERI signature covers — sorted keys,
    no whitespace, UTF-8 (the `applications._package_bytes` idiom). What is
    signed is the report's DATA, never a rendered Excel/PDF (plan "scope
    cuts") — `report.data` is already JSONB (str/int/float/None/dict/list
    only, never `Decimal`), so this needs no coercion pass."""
    payload = {
        "report_id": str(report.id),
        "form_id": str(report.form_id),
        "organization_id": str(report.organization_id),
        "period_start": report.period_start.isoformat(),
        "period_end": report.period_end.isoformat(),
        "version_no": report.version_no,
        "data": report.data,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


# --- report_forms ----------------------------------------------------------


async def get_form(db: AsyncSession, form_id: uuid.UUID) -> ReportForm:
    form = await repo.get_form(db, form_id)
    if form is None:
        raise err("ERR-SYS-003", details={"form_id": str(form_id)})
    return form


async def list_forms(
    db: AsyncSession, *, status: str | None, params: PageParams
) -> tuple[list[ReportForm], int]:
    return await repo.list_forms(db, status=status, offset=params.offset, limit=params.page_size)


async def create_form(
    db: AsyncSession,
    *,
    code: str,
    version: int,
    name: dict[str, str],
    activity_type_id: uuid.UUID | None,
    period_type: str,
    columns: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    schedule: dict[str, Any],
    valid_from: Any,
    actor: User,
) -> ReportForm:
    existing = await repo.get_form_by_code_version(db, code=code, version=version)
    if existing is not None:
        raise err(
            "ERR-REP-001",
            details={"reason": "form_version_exists", "code": code, "version": version},
        )
    form = ReportForm(
        code=code,
        version=version,
        name=name,
        activity_type_id=activity_type_id,
        period_type=period_type,
        columns=columns,
        rules=rules,
        schedule=schedule,
        status="draft",
        valid_from=valid_from,
        created_by=actor.id,
    )
    db.add(form)
    await db.flush()
    await audit.log(
        db,
        action=FORM_CREATE,
        user_id=actor.id,
        object_type="report_form",
        object_id=form.id,
        new_value={"code": code, "version": version},
    )
    return form


async def activate_form(db: AsyncSession, form_id: uuid.UUID, actor: User) -> ReportForm:
    form = await get_form(db, form_id)
    if form.status != "draft":
        raise err("ERR-REP-001", details={"reason": "not_draft", "status": form.status})
    form.status = "active"
    await audit.log(
        db,
        action=FORM_ACTIVATE,
        user_id=actor.id,
        object_type="report_form",
        object_id=form.id,
        old_value={"status": "draft"},
        new_value={"status": "active"},
    )
    # `updated_at`'s `onupdate=func.now()` is only known once flushed — a
    # `response_model` serializing this ORM row right after (never awaited
    # itself) cannot lazily fetch it (lesson: MissingGreenlet on a
    # server-generated column touched outside an explicit async round trip).
    await db.flush()
    return form


async def archive_form(db: AsyncSession, form_id: uuid.UUID, actor: User) -> ReportForm:
    form = await get_form(db, form_id)
    if form.status == "archived":
        raise err("ERR-REP-001", details={"reason": "already_archived"})
    old_status = form.status
    form.status = "archived"
    await audit.log(
        db,
        action=FORM_ARCHIVE,
        user_id=actor.id,
        object_type="report_form",
        object_id=form.id,
        old_value={"status": old_status},
        new_value={"status": "archived"},
    )
    await db.flush()
    return form


# --- reports: lifecycle ------------------------------------------------


async def get_report(db: AsyncSession, report_id: uuid.UUID, actor: User) -> Report:
    report = await repo.get_report(db, report_id)
    if report is None:
        raise err("ERR-SYS-003", details={"report_id": str(report_id)})
    org = await db.get(Organization, report.organization_id)
    assert org is not None
    if not _in_zone(zone_of(actor), org):
        raise err("ERR-ACL-002")
    return report


async def list_reports(
    db: AsyncSession,
    *,
    actor: User,
    organization_id: uuid.UUID | None,
    status: str | None,
    form_id: uuid.UUID | None,
    params: PageParams,
) -> tuple[list[Report], int]:
    zone = zone_filter(
        zone_of(actor),
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=Report.organization_id,
    )
    return await repo.list_reports(
        db,
        zone=zone,
        organization_id=organization_id,
        status=status,
        form_id=form_id,
        offset=params.offset,
        limit=params.page_size,
    )


async def create_report(
    db: AsyncSession,
    *,
    form_id: uuid.UUID,
    organization_id: uuid.UUID,
    period_start: Any,
    period_end: Any,
    actor: User,
) -> Report:
    form = await get_form(db, form_id)
    if form.status != "active":
        raise err("ERR-REP-003", details={"reason": "form_not_active", "status": form.status})
    _assert_zone(actor, organization_id)

    report = Report(
        form_id=form_id,
        organization_id=organization_id,
        period_start=period_start,
        period_end=period_end,
        version_no=1,
        status="created",
        data={},
        created_by=actor.id,
    )
    try:
        async with db.begin_nested():
            db.add(report)
            await db.flush()
    except IntegrityError as exc:
        cause = exc.orig.__cause__ if exc.orig is not None else None
        if getattr(cause, "constraint_name", None) != "uq_reports_form_org_period_version":
            raise
        raise err("ERR-REP-001", details={"reason": "duplicate_period"}) from exc

    await audit.log(
        db,
        action=REPORT_CREATE,
        user_id=actor.id,
        object_type=OBJECT_TYPE,
        object_id=report.id,
        new_value={
            "form_id": str(form_id),
            "organization_id": str(organization_id),
            "period_start": str(period_start),
        },
    )
    return report


async def generate_report(db: AsyncSession, report_id: uuid.UUID, actor: User) -> Report:
    """(Re)compute `data["rows"]` from `repo.report_rows` — the cross-module
    reader join. Overwrites whatever rows were there; a hodim's own manual
    edits (`update_report_data`) are expected to run AFTER this, not before."""
    report = await repo.get_report_for_update(db, report_id)
    if report is None:
        raise err("ERR-SYS-003", details={"report_id": str(report_id)})
    _assert_zone(actor, report.organization_id)
    _assert_editable(report)

    form = await get_form(db, report.form_id)
    rows = await repo.report_rows(
        db,
        organization_id=report.organization_id,
        activity_type_id=form.activity_type_id,
        period_start=report.period_start,
        period_end=report.period_end,
    )
    report.data = {"rows": rows, "generated_at": datetime.now(UTC).isoformat()}
    report.filled_by = actor.id
    await audit.log(
        db,
        action=REPORT_GENERATE,
        user_id=actor.id,
        object_type=OBJECT_TYPE,
        object_id=report.id,
        new_value={"row_count": len(rows)},
    )
    await db.flush()
    await db.refresh(report)  # see TRACK-REPORT.md: an explicit refresh, not the implicit one
    return report


async def update_report_data(
    db: AsyncSession, report_id: uuid.UUID, *, rows: list[dict[str, Any]], actor: User
) -> Report:
    report = await repo.get_report_for_update(db, report_id)
    if report is None:
        raise err("ERR-SYS-003", details={"report_id": str(report_id)})
    _assert_zone(actor, report.organization_id)
    _assert_editable(report)

    report.data = {**report.data, "rows": rows}
    report.filled_by = actor.id
    await audit.log(
        db,
        action=REPORT_UPDATE_DATA,
        user_id=actor.id,
        object_type=OBJECT_TYPE,
        object_id=report.id,
        new_value={"row_count": len(rows)},
    )
    await db.flush()
    await db.refresh(report)  # see TRACK-REPORT.md: an explicit refresh, not the implicit one
    return report


async def submit_report(db: AsyncSession, report_id: uuid.UUID, actor: User) -> Report:
    """`created`/`returned` -> `submitted`, gated on `rules.check_rows`
    (tz/04 С20: "логические проверки"). No preview route exists for a
    report today, so a violation is always a refusal — see `rules.py`'s own
    docstring for why the checker still returns a full list rather than
    raising on the first one."""
    report = await repo.get_report_for_update(db, report_id)
    if report is None:
        raise err("ERR-SYS-003", details={"report_id": str(report_id)})
    _assert_zone(actor, report.organization_id)
    _assert_editable(report)

    rows = report.data.get("rows", [])
    violations = check_rows(rows)
    if violations:
        raise err("ERR-REP-002", details={"checks": violations})

    from_status = report.status
    report.status = "submitted"
    report.returned_by = None
    report.returned_comment = None
    report.filled_by = actor.id
    report.submitted_at = datetime.now(UTC)
    await audit.log(
        db,
        action=REPORT_SUBMIT,
        user_id=actor.id,
        object_type=OBJECT_TYPE,
        object_id=report.id,
        old_value={"status": from_status},
        new_value={"status": "submitted"},
    )
    await db.flush()
    await db.refresh(report)  # see TRACK-REPORT.md: an explicit refresh, not the implicit one
    return report


async def sign_report(
    db: AsyncSession,
    report_id: uuid.UUID,
    *,
    pkcs7: str,
    actor: User,
    ip: str | None = None,
) -> Report:
    """`submitted` -> `head_approved`, the rahbar's ERI signature
    (purpose=`report_approve`, design/02). Nothing of ours is pending when
    `sign()` is called — its transaction contract commits the caller's WHOLE
    session on every refusal, and `report` has had no field written yet at
    this point (`applications`/`permits`' own ordering rule)."""
    report = await repo.get_report_for_update(db, report_id)
    if report is None:
        raise err("ERR-SYS-003", details={"report_id": str(report_id)})
    if report.status != "submitted":
        raise err("ERR-REP-001", details={"reason": "not_submitted", "status": report.status})
    await _assert_report_signer(db, report, actor)

    await signatures_service.sign(
        db,
        object_type=OBJECT_TYPE,
        object_id=report.id,
        purpose=REPORT_APPROVE_PURPOSE,
        document=_report_bytes(report),
        pkcs7=pkcs7,
        user=actor,
        ip=ip,
    )

    report.status = "head_approved"
    await audit.log(
        db,
        action=REPORT_SIGN,
        user_id=actor.id,
        object_type=OBJECT_TYPE,
        object_id=report.id,
        old_value={"status": "submitted"},
        new_value={"status": "head_approved"},
    )
    await db.flush()
    await db.refresh(report)  # see TRACK-REPORT.md: an explicit refresh, not the implicit one
    return report


async def return_report(
    db: AsyncSession, report_id: uuid.UUID, *, comment: str, actor: User
) -> Report:
    """Plan ruling 3: a return re-enters the SAME row. `submitted` is
    returned by the rahbar (`returned_by="head"`, the same identity check as
    `sign_report` — `reports.sign` alone is not enough, see
    `_assert_report_signer`); `head_approved` is returned by the central
    office (`returned_by="center"`) — `reports.accept` is central-only by
    construction (migration 0027's seed), but the ROUTE gates this whole
    action on `require_any_permission(reports.sign, reports.accept)` for the
    `submitted` branch's sake, so a `reports.sign` holder (every executor_head
    nationally) reaches this branch too and needs its OWN check that the
    actor actually holds `reports.accept` — see `_assert_may_return_centrally`.
    """
    report = await repo.get_report_for_update(db, report_id)
    if report is None:
        raise err("ERR-SYS-003", details={"report_id": str(report_id)})

    if report.status == "submitted":
        await _assert_report_signer(db, report, actor)
        returned_by = "head"
    elif report.status == "head_approved":
        await _assert_may_return_centrally(db, report, actor)
        returned_by = "center"
    else:
        raise err("ERR-REP-001", details={"reason": "not_returnable", "status": report.status})

    from_status = report.status
    report.status = "returned"
    report.returned_by = returned_by
    report.returned_comment = comment
    await audit.log(
        db,
        action=REPORT_RETURN,
        user_id=actor.id,
        object_type=OBJECT_TYPE,
        object_id=report.id,
        old_value={"status": from_status},
        new_value={"status": "returned", "returned_by": returned_by},
    )
    await db.flush()
    await db.refresh(report)  # see TRACK-REPORT.md: an explicit refresh, not the implicit one
    return report


async def approve_report(db: AsyncSession, report_id: uuid.UUID, actor: User) -> Report:
    """`head_approved` -> `approved` — the terminal, database-frozen state
    (module docstring / plan ruling 1). Central-office-only by construction
    (`reports.accept`), same reasoning as `return_report`'s center branch."""
    report = await repo.get_report_for_update(db, report_id)
    if report is None:
        raise err("ERR-SYS-003", details={"report_id": str(report_id)})
    if report.status != "head_approved":
        raise err("ERR-REP-001", details={"reason": "not_head_approved", "status": report.status})

    report.status = "approved"
    report.approved_by = actor.id
    report.approved_at = datetime.now(UTC)
    await audit.log(
        db,
        action=REPORT_APPROVE,
        user_id=actor.id,
        object_type=OBJECT_TYPE,
        object_id=report.id,
        old_value={"status": "head_approved"},
        new_value={"status": "approved"},
    )
    await db.flush()
    await db.refresh(report)  # see TRACK-REPORT.md: an explicit refresh, not the implicit one
    return report


async def revise_report(db: AsyncSession, report_id: uuid.UUID, actor: User) -> Report:
    """A correction after approval: a NEW row, `version_no + 1`,
    `parent_report_id` set to the approved row it corrects — never an edit
    in place (the database trigger would refuse one anyway). Seeds the new
    row's `data` from the parent's own, as a starting point for the hodim to
    edit rather than an empty report."""
    parent = await repo.get_report_for_update(db, report_id)
    if parent is None:
        raise err("ERR-SYS-003", details={"report_id": str(report_id)})
    _assert_zone(actor, parent.organization_id)
    if parent.status != "approved":
        raise err("ERR-REP-001", details={"reason": "not_approved", "status": parent.status})

    revision = Report(
        form_id=parent.form_id,
        organization_id=parent.organization_id,
        period_start=parent.period_start,
        period_end=parent.period_end,
        version_no=parent.version_no + 1,
        parent_report_id=parent.id,
        status="created",
        data=parent.data,
        created_by=actor.id,
    )
    try:
        async with db.begin_nested():
            db.add(revision)
            await db.flush()
    except IntegrityError as exc:
        cause = exc.orig.__cause__ if exc.orig is not None else None
        if getattr(cause, "constraint_name", None) != "uq_reports_form_org_period_version":
            raise
        raise err("ERR-REP-001", details={"reason": "duplicate_period"}) from exc

    await audit.log(
        db,
        action=REPORT_REVISE,
        user_id=actor.id,
        object_type=OBJECT_TYPE,
        object_id=revision.id,
        new_value={"parent_report_id": str(parent.id), "version_no": revision.version_no},
    )
    return revision


# --- export ------------------------------------------------------------


async def export_excel(db: AsyncSession, report_id: uuid.UUID, actor: User) -> bytes:
    report = await get_report(db, report_id, actor)
    form = await get_form(db, report.form_id)
    return render.render_excel(report, form)


async def export_pdf(db: AsyncSession, report_id: uuid.UUID, actor: User) -> bytes:
    report = await get_report(db, report_id, actor)
    form = await get_form(db, report.form_id)
    return render.render_pdf(report, form)
