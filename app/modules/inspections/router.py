"""HTTP routes for inspections (tz/04 С15/С16). Fine-grained "own OR a specific
permission" gates (starting one's own task, submitting one's own explanation,
filing one's own appeal) are NOT expressed as a route-level `require_permission`
— they gate on `get_current_user` alone and the SERVICE checks ownership,
because the actual rule ("assignee" / "the case's own applicant") is not a
permission code at all (lesson: zone/ownership scoping is a separate question
from "may this role act at all")."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import xlsx
from app.core.deps import get_db
from app.core.schemas import Page, PageParams
from app.core.time import business_today
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.inspections import export, service
from app.modules.inspections.permissions import (
    ACTS_WRITE,
    CASES_MANAGE,
    CHECKLISTS_MANAGE,
    TASKS_MANAGE,
)
from app.modules.inspections.schemas import (
    ActCardOut,
    ActCreateIn,
    ActFileIn,
    ActFileOut,
    ActOut,
    ActSignIn,
    ActUpdateIn,
    AppealIn,
    AppealOut,
    AppealResolveIn,
    CaseCardOut,
    CaseOut,
    ChecklistIn,
    ChecklistOut,
    DecisionIn,
    ExplanationIn,
    ReassignIn,
    TaskIn,
    TaskOut,
)

router = APIRouter(prefix="/inspections", tags=["inspections"])

AsyncDb = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def _checklist_items_json(payload: ChecklistIn) -> list[dict[str, object]]:
    return [
        {
            "code": item.code,
            "question": item.question.root,
            "type": item.type,
            "required": item.required,
        }
        for item in payload.items
    ]


@router.post("/checklists", status_code=201)
async def create_checklist(
    payload: ChecklistIn,
    db: AsyncDb,
    user: Annotated[User, Depends(require_permission(CHECKLISTS_MANAGE))],
) -> ChecklistOut:
    checklist = await service.create_checklist(
        db,
        code=payload.code,
        name=payload.name.root,
        activity_type_id=payload.activity_type_id,
        items=_checklist_items_json(payload),
        actor=user,
    )
    return ChecklistOut.model_validate(checklist)


@router.get("/checklists")
async def list_checklists(db: AsyncDb, user: CurrentUser) -> list[ChecklistOut]:
    items = await service.list_checklists(db)
    return [ChecklistOut.model_validate(item) for item in items]


@router.post("/tasks", status_code=201)
async def create_task(
    payload: TaskIn,
    db: AsyncDb,
    user: Annotated[User, Depends(require_permission(TASKS_MANAGE))],
) -> TaskOut:
    task = await service.create_task(
        db,
        kind=payload.kind,
        application_id=payload.application_id,
        permit_id=payload.permit_id,
        contour_id=payload.contour_id,
        assigned_to=payload.assigned_to,
        due_at=payload.due_at,
        actor=user,
    )
    return TaskOut.model_validate(task)


@router.get("/tasks")
async def list_tasks(
    db: AsyncDb,
    user: CurrentUser,
    params: Annotated[PageParams, Depends()],
    status: str | None = None,
) -> Page[TaskOut]:
    items, total = await service.list_tasks(db, status=status, params=params, actor=user)
    return Page[TaskOut](
        items=[TaskOut.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/tasks/export.xlsx")
async def export_tasks_xlsx(
    db: AsyncDb,
    user: CurrentUser,
    lang: xlsx.Lang = "uz_latn",
    status: str | None = None,
) -> Response:
    """`GET /tasks` as a spreadsheet (stage 13, ruling #204): the same scope,
    the same filter, every matching row up to the configured cap. Declared
    before `/tasks/{task_id}` on purpose — `export.xlsx` is not a UUID, and
    a 404 here beats the 422 the UUID parser would otherwise answer."""
    items, total, cap = await export.task_rows(db, actor=user, lang=lang, status=status)
    filename = f"inspection-tasks-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render_tasks(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.get("/tasks/{task_id}")
async def get_task(task_id: uuid.UUID, db: AsyncDb, user: CurrentUser) -> TaskOut:
    task = await service.get_task(db, task_id, actor=user)
    return TaskOut.model_validate(task)


@router.post("/tasks/{task_id}/start")
async def start_task(task_id: uuid.UUID, db: AsyncDb, user: CurrentUser) -> TaskOut:
    task = await service.start_task(db, task_id, actor=user)
    return TaskOut.model_validate(task)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: uuid.UUID,
    db: AsyncDb,
    user: Annotated[User, Depends(require_permission(TASKS_MANAGE))],
) -> TaskOut:
    task = await service.cancel_task(db, task_id, actor=user)
    return TaskOut.model_validate(task)


@router.post("/tasks/{task_id}/reassign")
async def reassign_task(
    task_id: uuid.UUID,
    payload: ReassignIn,
    db: AsyncDb,
    user: Annotated[User, Depends(require_permission(TASKS_MANAGE))],
) -> TaskOut:
    task = await service.reassign_task(
        db, task_id, new_assignee_id=payload.new_assignee_id, actor=user
    )
    return TaskOut.model_validate(task)


@router.post("/acts", status_code=201)
async def create_act(
    payload: ActCreateIn,
    db: AsyncDb,
    user: Annotated[User, Depends(require_permission(ACTS_WRITE))],
) -> ActOut:
    act = await service.create_act(
        db,
        task_id=payload.task_id,
        permit_id=payload.permit_id,
        application_id=payload.application_id,
        occurred_at=payload.occurred_at,
        gps=(payload.gps.lon, payload.gps.lat) if payload.gps is not None else None,
        gps_accuracy_m=payload.gps_accuracy_m,
        checklist_id=payload.checklist_id,
        answers=payload.answers,
        facts=payload.facts,
        notes=payload.notes,
        result=payload.result,
        created_offline_at=payload.created_offline_at,
        actor=user,
    )
    return ActOut.model_validate(act)


@router.get("/acts")
async def list_acts(
    db: AsyncDb,
    user: CurrentUser,
    params: Annotated[PageParams, Depends()],
    result: str | None = None,
) -> Page[ActOut]:
    items, total = await service.list_acts(db, result=result, params=params, actor=user)
    return Page[ActOut](
        items=[ActOut.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/acts/export.xlsx")
async def export_acts_xlsx(
    db: AsyncDb,
    user: CurrentUser,
    lang: xlsx.Lang = "uz_latn",
    result: str | None = None,
) -> Response:
    """`GET /acts` as a spreadsheet — same scope, same filter, declared
    before `/acts/{act_id}` for the same reason `export_tasks_xlsx` is."""
    items, total, cap = await export.act_rows(db, actor=user, lang=lang, result=result)
    filename = f"inspection-acts-{business_today().isoformat()}.xlsx"
    return xlsx.xlsx_response(
        export.render_acts(items, lang=lang), filename=filename, total=total, cap=cap
    )


@router.get("/acts/{act_id}")
async def get_act(act_id: uuid.UUID, db: AsyncDb, user: CurrentUser) -> ActCardOut:
    card = await service.act_card(db, act_id, actor=user)
    return ActCardOut.build(card)


@router.patch("/acts/{act_id}")
async def update_act(
    act_id: uuid.UUID,
    payload: ActUpdateIn,
    db: AsyncDb,
    user: Annotated[User, Depends(require_permission(ACTS_WRITE))],
) -> ActOut:
    act = await service.update_act(
        db,
        act_id,
        occurred_at=payload.occurred_at,
        gps=(payload.gps.lon, payload.gps.lat) if payload.gps is not None else None,
        gps_accuracy_m=payload.gps_accuracy_m,
        answers=payload.answers,
        facts=payload.facts,
        notes=payload.notes,
        result=payload.result,
        actor=user,
    )
    return ActOut.model_validate(act)


@router.post("/acts/{act_id}/files", status_code=201)
async def attach_act_file(
    act_id: uuid.UUID,
    payload: ActFileIn,
    db: AsyncDb,
    user: Annotated[User, Depends(require_permission(ACTS_WRITE))],
) -> ActFileOut:
    link = await service.attach_act_file(
        db,
        act_id,
        file_id=payload.file_id,
        kind=payload.kind,
        taken_at=payload.taken_at,
        gps=(payload.gps.lon, payload.gps.lat) if payload.gps is not None else None,
        device=payload.device,
        actor=user,
    )
    return ActFileOut.model_validate(link)


@router.post("/acts/{act_id}/sign")
async def sign_act(
    act_id: uuid.UUID,
    payload: ActSignIn,
    request: Request,
    db: AsyncDb,
    user: Annotated[User, Depends(require_permission(ACTS_WRITE))],
) -> ActOut:
    act = await service.sign_act(
        db,
        act_id,
        pkcs7=payload.pkcs7,
        violation_type_item_id=payload.violation_type_item_id,
        actor=user,
        ip=request.client.host if request.client else None,
    )
    return ActOut.model_validate(act)


@router.get("/cases")
async def list_cases(
    db: AsyncDb,
    user: CurrentUser,
    params: Annotated[PageParams, Depends()],
    status: str | None = None,
    applicant_id: uuid.UUID | None = None,
) -> Page[CaseOut]:
    """`applicant_id` (ruling R8, finding F3): every case against ONE
    violator, for anyone who may already see those cases — the filter runs
    INSIDE `_case_scope`, so a leshoz head still sees only their own zone's
    cases against that applicant, never another oblast's."""
    items, total = await service.list_cases(
        db, status=status, applicant_id=applicant_id, params=params, actor=user
    )
    return Page[CaseOut](
        items=[CaseOut.model_validate(item) for item in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/cases/{case_id}")
async def get_case(case_id: uuid.UUID, db: AsyncDb, user: CurrentUser) -> CaseCardOut:
    card = await service.case_card(db, case_id, actor=user)
    return CaseCardOut.build(card)


@router.post("/cases/{case_id}/request-explanation")
async def request_explanation(case_id: uuid.UUID, db: AsyncDb, user: CurrentUser) -> CaseOut:
    case = await service.request_explanation(db, case_id, actor=user)
    return CaseOut.model_validate(case)


@router.post("/cases/{case_id}/explanation")
async def submit_explanation(
    case_id: uuid.UUID, payload: ExplanationIn, db: AsyncDb, user: CurrentUser
) -> CaseOut:
    case = await service.submit_explanation(
        db, case_id, text=payload.text, file_id=payload.file_id, actor=user
    )
    return CaseOut.model_validate(case)


@router.post("/cases/{case_id}/decide")
async def decide_case(
    case_id: uuid.UUID,
    payload: DecisionIn,
    db: AsyncDb,
    user: Annotated[User, Depends(require_permission(CASES_MANAGE))],
) -> CaseOut:
    case = await service.decide_case(
        db,
        case_id,
        decision=payload.decision,
        damage_amount=payload.damage_amount,
        damage_calc=payload.damage_calc,
        note=payload.note,
        actor=user,
    )
    return CaseOut.model_validate(case)


@router.post("/cases/{case_id}/appeal", status_code=201)
async def appeal_case(
    case_id: uuid.UUID, payload: AppealIn, db: AsyncDb, user: CurrentUser
) -> AppealOut:
    appeal = await service.appeal_case(db, case_id, text=payload.text, actor=user)
    return AppealOut.model_validate(appeal)


@router.post("/cases/{case_id}/appeal/resolve")
async def resolve_appeal(
    case_id: uuid.UUID,
    payload: AppealResolveIn,
    db: AsyncDb,
    user: Annotated[User, Depends(require_permission(CASES_MANAGE))],
) -> AppealOut:
    appeal = await service.resolve_appeal(db, case_id, result=payload.result, actor=user)
    return AppealOut.model_validate(appeal)


@router.post("/cases/{case_id}/close")
async def close_case(
    case_id: uuid.UUID,
    db: AsyncDb,
    user: Annotated[User, Depends(require_permission(CASES_MANAGE))],
) -> CaseOut:
    case = await service.close_case(db, case_id, actor=user)
    return CaseOut.model_validate(case)
