"""HTTP routes for `reports`: the form catalog and the report lifecycle
(tz/04 С20). Thin — parsing, `Depends`, calling `service` (design/01 rule 1).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files, xlsx
from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.auth.deps import require_any_permission, require_permission
from app.modules.auth.models import User
from app.modules.reports import export, service
from app.modules.reports.models import REPORT_STATUSES
from app.modules.reports.permissions import (
    REPORTS_ACCEPT,
    REPORTS_FORMS_MANAGE,
    REPORTS_MANAGE,
    REPORTS_SIGN,
    REPORTS_VIEW,
)
from app.modules.reports.schemas import (
    ReportCreate,
    ReportDataUpdate,
    ReportFormCreate,
    ReportFormOut,
    ReportOut,
    ReportReturnIn,
    ReportSignIn,
)

router = APIRouter(prefix="/reports", tags=["reports"])


# --- report_forms ------------------------------------------------------


@router.post("/forms", response_model=ReportFormOut, status_code=201)
async def create_form(
    data: ReportFormCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_FORMS_MANAGE))],
) -> ReportFormOut:
    form = await service.create_form(
        db,
        code=data.code,
        version=data.version,
        name=data.name.model_dump(exclude_none=True),
        activity_type_id=data.activity_type_id,
        period_type=data.period_type,
        columns=[column.model_dump() for column in data.columns],
        rules=data.rules,
        schedule=data.schedule,
        valid_from=data.valid_from,
        actor=user,
    )
    return ReportFormOut.model_validate(form)


@router.get("/forms", response_model=Page[ReportFormOut])
async def list_forms(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_permission(REPORTS_VIEW))],
    params: Annotated[PageParams, Depends()],
    status: Annotated[str | None, Query()] = None,
) -> Page[ReportFormOut]:
    items, total = await service.list_forms(db, status=status, params=params)
    return Page[ReportFormOut](
        items=[ReportFormOut.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/forms/export.xlsx")
async def export_report_forms_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_permission(REPORTS_VIEW))],
    lang: xlsx.Lang = "uz_latn",
    status: Annotated[str | None, Query()] = None,
) -> Response:
    """`GET /reports/forms` as a spreadsheet (stage 13, ruling #204).
    Declared before `/forms/{form_id}` on purpose — a UUID path parser
    would otherwise answer this literal path with a worse error than a 404."""
    items, total, cap = await export.form_rows(db, lang=lang, status=status)
    filename = f"hisobot-shakllari-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render_forms(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.get("/forms/{form_id}", response_model=ReportFormOut)
async def get_form(
    form_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_permission(REPORTS_VIEW))],
) -> ReportFormOut:
    return ReportFormOut.model_validate(await service.get_form(db, form_id))


@router.post("/forms/{form_id}/activate", response_model=ReportFormOut)
async def activate_form(
    form_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_FORMS_MANAGE))],
) -> ReportFormOut:
    return ReportFormOut.model_validate(await service.activate_form(db, form_id, user))


@router.post("/forms/{form_id}/archive", response_model=ReportFormOut)
async def archive_form(
    form_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_FORMS_MANAGE))],
) -> ReportFormOut:
    return ReportFormOut.model_validate(await service.archive_form(db, form_id, user))


# --- reports -------------------------------------------------------------


@router.post("", response_model=ReportOut, status_code=201)
async def create_report(
    data: ReportCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_MANAGE))],
) -> ReportOut:
    report = await service.create_report(
        db,
        form_id=data.form_id,
        organization_id=data.organization_id,
        period_start=data.period_start,
        period_end=data.period_end,
        actor=user,
    )
    return ReportOut.model_validate(report)


@router.get("", response_model=Page[ReportOut])
async def list_reports(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_VIEW))],
    params: Annotated[PageParams, Depends()],
    organization_id: Annotated[uuid.UUID | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    form_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[ReportOut]:
    if status is not None and status not in REPORT_STATUSES:
        status = None  # an unknown status filters to nothing usable — ignored, not 500'd
    items, total = await service.list_reports(
        db,
        actor=user,
        organization_id=organization_id,
        status=status,
        form_id=form_id,
        params=params,
    )
    return Page[ReportOut](
        items=[ReportOut.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/export.xlsx")
async def export_reports_xlsx(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_VIEW))],
    lang: xlsx.Lang = "uz_latn",
    organization_id: Annotated[uuid.UUID | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    form_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Response:
    """`GET /reports` as a spreadsheet (stage 13, ruling #204): the same
    filters, the same zone (ruling R2: `service.list_reports`, the exact
    function the list route calls), every matching row up to the configured
    cap. Declared before `/{report_id}` on purpose — a UUID path parser
    would otherwise answer this literal path with a worse error than a 404.
    `GET /{report_id}/export.xlsx` (the per-report data export) is untouched."""
    if status is not None and status not in REPORT_STATUSES:
        status = None  # mirrors list_reports' own "an unknown status filters to nothing usable"
    items, total, cap = await export.report_rows(
        db, actor=user, lang=lang, organization_id=organization_id, status=status, form_id=form_id
    )
    filename = f"hisobotlar-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render_reports(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.get("/{report_id}", response_model=ReportOut)
async def get_report(
    report_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_VIEW))],
) -> ReportOut:
    return ReportOut.model_validate(await service.get_report(db, report_id, user))


@router.post("/{report_id}/generate", response_model=ReportOut)
async def generate_report(
    report_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_MANAGE))],
) -> ReportOut:
    return ReportOut.model_validate(await service.generate_report(db, report_id, user))


@router.patch("/{report_id}/data", response_model=ReportOut)
async def update_report_data(
    report_id: uuid.UUID,
    data: ReportDataUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_MANAGE))],
) -> ReportOut:
    report = await service.update_report_data(db, report_id, rows=data.rows, actor=user)
    return ReportOut.model_validate(report)


@router.post("/{report_id}/submit", response_model=ReportOut)
async def submit_report(
    report_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_MANAGE))],
) -> ReportOut:
    return ReportOut.model_validate(await service.submit_report(db, report_id, user))


@router.post("/{report_id}/sign", response_model=ReportOut)
async def sign_report(
    report_id: uuid.UUID,
    data: ReportSignIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_SIGN))],
) -> ReportOut:
    report = await service.sign_report(
        db,
        report_id,
        pkcs7=data.pkcs7,
        actor=user,
        ip=request.client.host if request.client else None,
    )
    return ReportOut.model_validate(report)


@router.post("/{report_id}/return", response_model=ReportOut)
async def return_report(
    report_id: uuid.UUID,
    data: ReportReturnIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_any_permission(REPORTS_SIGN, REPORTS_ACCEPT))],
) -> ReportOut:
    report = await service.return_report(db, report_id, comment=data.comment, actor=user)
    return ReportOut.model_validate(report)


@router.post("/{report_id}/approve", response_model=ReportOut)
async def approve_report(
    report_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_ACCEPT))],
) -> ReportOut:
    return ReportOut.model_validate(await service.approve_report(db, report_id, user))


@router.post("/{report_id}/revise", response_model=ReportOut, status_code=201)
async def revise_report(
    report_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_MANAGE))],
) -> ReportOut:
    return ReportOut.model_validate(await service.revise_report(db, report_id, user))


# --- export --------------------------------------------------------------


@router.get("/{report_id}/export.xlsx")
async def export_report_excel(
    report_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_VIEW))],
) -> Response:
    data = await service.export_excel(db, report_id, user)
    filename = f"report-{report_id}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": files.content_disposition("attachment", filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{report_id}/export.pdf")
async def export_report_pdf(
    report_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission(REPORTS_VIEW))],
) -> Response:
    data = await service.export_pdf(db, report_id, user)
    filename = f"report-{report_id}.pdf"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": files.content_disposition("attachment", filename),
            "X-Content-Type-Options": "nosniff",
        },
    )
