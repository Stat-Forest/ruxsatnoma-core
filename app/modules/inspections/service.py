"""Inspections service (design/02 § inspections, plan `04.1-inspections`):
assignments, field acts, and violation cases — tz/04 С15/С16.

Level 5 (design/01: "add-ons and channels") — reaches `applications`, `permits`
and `gis` only through their own `service`, never their `repo`/`models`
(module boundary rule). `MediaFile` is the one exception, a level-0 CORE model
several other modules already read directly (`applications._own_document_file`,
`permits.service`'s own imports) — crossing no module boundary.

Every write is additionally zone-scoped through `organization_id` (resolved
once, at creation — see `models.py`'s own docstring), the same two-gate shape
(permission answers "at all", zone answers "whose rows") every module in this
codebase uses.
"""

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from geoalchemy2.elements import WKTElement
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files
from app.core.abac import Zone, zone_filter, zone_of
from app.core.errors import err
from app.core.models import MediaFile
from app.core.numbers import next_public_number
from app.core.schemas import PageParams
from app.core.time import add_working_days, business_today
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import ClassifierItem, Organization
from app.modules.applications import service as applications_service
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.inspections import repo
from app.modules.inspections.models import (
    Checklist,
    InspectionAct,
    InspectionActFile,
    InspectionTask,
    ViolationAppeal,
    ViolationCase,
    ViolationCaseHistory,
)
from app.modules.inspections.permissions import ACTS_WRITE, CASES_MANAGE, TASKS_MANAGE, VIEW_ANY
from app.modules.permits import service as permits_service
from app.modules.signatures import service as signatures_service

# Public numbers: `core/models.py`'s own docstring already reserves "VC" for
# this module ("RX/INV/VC/MR/ST/ChT").
NUMBER_PREFIX = "VC"

VIOLATION_TYPES_CLASSIFIER_CODE = "violation_types"

# `signatures.service.sign`'s object type/purpose for a field act (design/02 §
# inspections: "The signature lives in signatures (purpose=act_sign)").
OBJECT_TYPE_ACT = "inspection_act"
ACT_SIGN_PURPOSE = "act_sign"

# Audit action codes: "<object>.<verb>" in English (CLAUDE.md, decision #38
# ruling 17) — audit is level 0 and knows no domain vocabulary.
CHECKLIST_CREATE = "checklist.create"
TASK_CREATE = "inspection_task.create"
TASK_START = "inspection_task.start"
TASK_CANCEL = "inspection_task.cancel"
TASK_COMPLETE = "inspection_task.complete"
ACT_CREATE = "inspection_act.create"
ACT_UPDATE = "inspection_act.update"
ACT_ATTACH_FILE = "inspection_act.attach_file"
ACT_SIGN = "inspection_act.sign"
CASE_OPEN = "violation_case.open"
CASE_REQUEST_EXPLANATION = "violation_case.request_explanation"
CASE_SUBMIT_EXPLANATION = "violation_case.submit_explanation"
CASE_DECIDE = "violation_case.decide"
CASE_APPEAL = "violation_case.appeal"
CASE_RESOLVE_APPEAL = "violation_case.resolve_appeal"
CASE_CLOSE = "violation_case.close"

TASK_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "assigned": ("in_progress", "done", "cancelled"),
    "in_progress": ("done", "cancelled"),
    "done": (),
    "cancelled": (),
}
CASE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "opened": ("explanation_requested", "decided"),
    "explanation_requested": ("explained", "decided"),
    "explained": ("decided",),
    "decided": ("appealed", "closed"),
    "appealed": ("decided",),
    "closed": (),
    "archived": (),
}


# --- permission/zone plumbing ------------------------------------------------
#
# Local copies of `_organization_in_zone`/`_organization_in_actor_zone` — the
# same shape `gis.service`/`norms.service`/`permits.service` each keep their
# own copy of, since the module boundary rules out importing a private helper
# from another module (lesson: "A `_client_for` fixture..." class; the
# canonical statement of the copy itself is `permits.service._organization_in_
# zone`'s own docstring).


def _organization_in_zone(zone: Zone, org: Organization) -> bool:
    if zone.region_id is not None and zone.region_id != org.region_id:
        return False
    if zone.district_id is not None and zone.district_id != org.district_id:
        return False
    if zone.organization_id is not None and zone.organization_id != org.id:
        return False
    return True


async def _organization_in_actor_zone(
    db: AsyncSession, actor: User, organization_id: uuid.UUID | None
) -> bool:
    zone = zone_of(actor)
    if zone == Zone(None, None, None):
        return True
    if organization_id is None:
        return False
    org = await admin_repo.get_organization(db, organization_id)
    return org is not None and _organization_in_zone(zone, org)


async def _assert_organization_in_zone(
    db: AsyncSession, actor: User, organization_id: uuid.UUID | None
) -> None:
    if not await _organization_in_actor_zone(db, actor, organization_id):
        raise err("ERR-ACL-002")


async def _holds(db: AsyncSession, actor: User, code: str) -> bool:
    """One permission code, `sys_admin` bypass included (`applications.
    service._holds`'s own idiom) — used for an in-handler check a route-level
    `require_permission` cannot express (e.g. "this OR ownership")."""
    if await auth_repo.role_code(db, actor) == SUPERUSER_ROLE:
        return True
    return code in await auth_repo.permission_codes(db, actor)


# --- checklists ---------------------------------------------------------------


async def list_checklists(db: AsyncSession) -> Sequence[Checklist]:
    return await repo.list_active_checklists(db)


async def get_checklist(db: AsyncSession, checklist_id: uuid.UUID) -> Checklist:
    checklist = await repo.get_checklist(db, checklist_id)
    if checklist is None:
        raise err("ERR-SYS-003")
    return checklist


async def create_checklist(
    db: AsyncSession,
    *,
    code: str,
    name: dict[str, str],
    activity_type_id: uuid.UUID | None,
    items: list[dict[str, Any]],
    actor: User,
) -> Checklist:
    """A NEW version of `code` — supersede by archive-then-insert (`admin.
    service.supersede_classifier_item`'s own idiom), never an in-place edit: a
    past act's `checklist_id` names the EXACT version it was answered against
    (`models.py`'s own docstring), so a later edit must not reinterpret it."""
    if (
        activity_type_id is not None
        and await admin_repo.get_activity_type(db, activity_type_id) is None
    ):
        raise err("ERR-SYS-003", details={"activity_type": str(activity_type_id)})
    codes_seen = {item["code"] for item in items}
    if len(codes_seen) != len(items):
        raise err("ERR-VAL-001", details={"reason": "duplicate_item_code"})

    existing = await repo.get_active_checklist_by_code(db, code)
    version = await repo.next_checklist_version(db, code)
    if existing is not None:
        existing.status = "archived"
        await db.flush()

    checklist = Checklist(
        code=code,
        version=version,
        name=name,
        activity_type_id=activity_type_id,
        items=items,
        created_by=actor.id,
    )
    db.add(checklist)
    await db.flush()
    await audit.log(
        db,
        action=CHECKLIST_CREATE,
        user_id=actor.id,
        object_type="checklist",
        object_id=checklist.id,
        new_value={"code": code, "version": version},
    )
    return checklist


def _assert_checklist_answers(checklist: Checklist, answers: dict[str, Any]) -> None:
    """`ERR-INSP-002`: every question the checklist's active version marks
    `required` must have a non-empty answer. A structural check only — it
    says nothing about whether the VALUE is plausible, the same limit
    `_assert_doc_type`-style membership checks state for themselves."""
    missing = [
        item["code"]
        for item in checklist.items
        if item.get("required") and answers.get(item["code"]) in (None, "")
    ]
    if missing:
        raise err("ERR-INSP-002", details={"missing": missing})


# --- tasks ---------------------------------------------------------------------


async def _resolve_task_organization(
    db: AsyncSession,
    *,
    application_id: uuid.UUID | None,
    permit_id: uuid.UUID | None,
    contour_id: uuid.UUID | None,
    actor: User,
) -> uuid.UUID | None:
    if application_id is not None:
        org_id = await applications_service.effective_organization(db, application_id)
        if org_id is not None:
            return org_id
    if permit_id is not None:
        permit = await permits_service.get(db, permit_id)
        if permit is not None:
            return permit.organization_id
    if contour_id is not None:
        org_id = await gis_service.contour_organization(db, contour_id)
        if org_id is not None:
            return org_id
    return actor.organization_id


async def create_task(
    db: AsyncSession,
    *,
    kind: str,
    application_id: uuid.UUID | None,
    permit_id: uuid.UUID | None,
    contour_id: uuid.UUID | None,
    assigned_to: uuid.UUID,
    due_at: date | None,
    actor: User,
) -> InspectionTask:
    """`POST /inspections/tasks` — the site visit of C6 or a field inspection
    of C15. `due_at` defaults to 2 working days out, the one deadline tz/04
    С6 names ("выезд ≤2 рабочих дней от задания"); a caller may set another."""
    if application_id is None and permit_id is None and contour_id is None:
        raise err("ERR-VAL-001", details={"reason": "missing_subject"})
    if application_id is not None and await applications_service.get(db, application_id) is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    if permit_id is not None and await permits_service.get(db, permit_id) is None:
        raise err("ERR-SYS-003", details={"permit": str(permit_id)})
    if contour_id is not None and await gis_service.contour_organization(db, contour_id) is None:
        raise err("ERR-SYS-003", details={"contour": str(contour_id)})
    if await auth_repo.get_user(db, assigned_to) is None:
        raise err("ERR-SYS-003", details={"assigned_to": str(assigned_to)})

    organization_id = await _resolve_task_organization(
        db, application_id=application_id, permit_id=permit_id, contour_id=contour_id, actor=actor
    )
    await _assert_organization_in_zone(db, actor, organization_id)

    task = InspectionTask(
        kind=kind,
        application_id=application_id,
        permit_id=permit_id,
        contour_id=contour_id,
        organization_id=organization_id,
        assigned_to=assigned_to,
        due_at=due_at or add_working_days(business_today(), 2),
        created_by=actor.id,
    )
    db.add(task)
    await db.flush()
    await audit.log(
        db,
        action=TASK_CREATE,
        user_id=actor.id,
        object_type="inspection_task",
        object_id=task.id,
        new_value={"kind": kind, "assigned_to": str(assigned_to)},
    )
    return task


async def _readable_task(db: AsyncSession, task_id: uuid.UUID, *, actor: User) -> InspectionTask:
    task = await repo.get_task(db, task_id)
    if task is None:
        raise err("ERR-SYS-003")
    if task.assigned_to == actor.id:
        return task
    if await _holds(db, actor, VIEW_ANY) or await _holds(db, actor, TASKS_MANAGE):
        await _assert_organization_in_zone(db, actor, task.organization_id)
        return task
    raise err("ERR-ACL-001")


async def get_task(db: AsyncSession, task_id: uuid.UUID, *, actor: User) -> InspectionTask:
    return await _readable_task(db, task_id, actor=actor)


async def _task_scope(db: AsyncSession, actor: User) -> ColumnElement[bool]:
    if await _holds(db, actor, VIEW_ANY) or await _holds(db, actor, TASKS_MANAGE):
        return zone_filter(zone_of(actor), organization_col=InspectionTask.organization_id)
    return InspectionTask.assigned_to == actor.id


async def list_tasks(
    db: AsyncSession, *, status: str | None, params: PageParams, actor: User
) -> tuple[Sequence[InspectionTask], int]:
    scope = await _task_scope(db, actor)
    return await repo.list_tasks(db, scope=scope, status=status, params=params)


def _assert_task_transition(task: InspectionTask, to_status: str) -> None:
    if to_status not in TASK_TRANSITIONS.get(task.status, ()):
        raise err(
            "ERR-INSP-001",
            details={"reason": "bad_transition", "from": task.status, "to": to_status},
        )


async def start_task(db: AsyncSession, task_id: uuid.UUID, *, actor: User) -> InspectionTask:
    task = await repo.get_task(db, task_id)
    if task is None:
        raise err("ERR-SYS-003")
    if task.assigned_to != actor.id:
        raise err("ERR-ACL-001")
    _assert_task_transition(task, "in_progress")
    task.status = "in_progress"
    await db.flush()
    await audit.log(
        db, action=TASK_START, user_id=actor.id, object_type="inspection_task", object_id=task.id
    )
    return task


async def cancel_task(db: AsyncSession, task_id: uuid.UUID, *, actor: User) -> InspectionTask:
    task = await repo.get_task(db, task_id)
    if task is None:
        raise err("ERR-SYS-003")
    if not await _holds(db, actor, TASKS_MANAGE):
        raise err("ERR-ACL-001")
    await _assert_organization_in_zone(db, actor, task.organization_id)
    _assert_task_transition(task, "cancelled")
    task.status = "cancelled"
    await db.flush()
    await audit.log(
        db, action=TASK_CANCEL, user_id=actor.id, object_type="inspection_task", object_id=task.id
    )
    return task


async def _complete_task(db: AsyncSession, task: InspectionTask, *, actor: User) -> None:
    """Called from `sign_act` — signing the field act IS the completion of the
    site visit it was raised for. A no-op on a task already `done`/
    `cancelled` (a second act against the same task, or a task with no
    act-shaped completion event at all) rather than a raised error: this is
    an internal side effect, not a request the caller can retry differently."""
    if task.status not in TASK_TRANSITIONS:
        return
    if "done" not in TASK_TRANSITIONS[task.status]:
        return
    task.status = "done"
    task.completed_at = datetime.now(UTC)
    await db.flush()
    await audit.log(
        db, action=TASK_COMPLETE, user_id=actor.id, object_type="inspection_task", object_id=task.id
    )


# --- field acts ------------------------------------------------------------


def _gps_point(gps: tuple[float, float] | None) -> Any | None:
    """`(lon, lat)` -> WKT, never a raw string built from unvalidated input —
    every caller's `gps` already passed through `schemas.GpsPoint`'s own
    bounds before it reaches here."""
    if gps is None:
        return None
    return WKTElement(f"POINT({gps[0]} {gps[1]})", srid=4326)


async def _resolve_act_organization(
    db: AsyncSession,
    *,
    task: InspectionTask | None,
    permit_id: uuid.UUID | None,
    application_id: uuid.UUID | None,
    actor: User,
) -> uuid.UUID | None:
    if task is not None:
        return task.organization_id
    if application_id is not None:
        org_id = await applications_service.effective_organization(db, application_id)
        if org_id is not None:
            return org_id
    if permit_id is not None:
        permit = await permits_service.get(db, permit_id)
        if permit is not None:
            return permit.organization_id
    return actor.organization_id


async def _distance_to_relevant_contour(
    db: AsyncSession,
    *,
    permit_id: uuid.UUID | None,
    task: InspectionTask | None,
    gps: tuple[float, float] | None,
) -> Decimal | None:
    if gps is None:
        return None
    contour_id: uuid.UUID | None = None
    if permit_id is not None:
        permit = await permits_service.get(db, permit_id)
        if permit is not None:
            contour_id = permit.contour_id
    elif task is not None and task.contour_id is not None:
        contour_id = task.contour_id
    if contour_id is None:
        return None
    return await gis_service.distance_to_contour_m(db, contour_id, lon=gps[0], lat=gps[1])


async def create_act(
    db: AsyncSession,
    *,
    task_id: uuid.UUID | None,
    permit_id: uuid.UUID | None,
    application_id: uuid.UUID | None,
    occurred_at: datetime,
    gps: tuple[float, float] | None,
    gps_accuracy_m: Decimal | None,
    checklist_id: uuid.UUID,
    answers: dict[str, Any],
    facts: dict[str, Any],
    notes: str | None,
    result: str | None,
    created_offline_at: datetime | None,
    actor: User,
) -> InspectionAct:
    """`POST /inspections/acts` — tz/04 С15. All three of `task_id`/
    `permit_id`/`application_id` unset plus a `gps` fix is an "activity
    without a permit" act (design/02); at least one of the four must be
    given, or there is nothing this act is even about."""
    if task_id is None and permit_id is None and application_id is None and gps is None:
        raise err("ERR-VAL-001", details={"reason": "missing_subject_or_location"})

    task: InspectionTask | None = None
    if task_id is not None:
        task = await repo.get_task(db, task_id)
        if task is None:
            raise err("ERR-SYS-003", details={"task": str(task_id)})
        if task.assigned_to != actor.id:
            raise err("ERR-ACL-001")
    if application_id is not None and await applications_service.get(db, application_id) is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    if permit_id is not None and await permits_service.get(db, permit_id) is None:
        raise err("ERR-SYS-003", details={"permit": str(permit_id)})

    checklist = await repo.get_checklist(db, checklist_id)
    if checklist is None or checklist.status != "active":
        raise err("ERR-VAL-001", details={"reason": "unknown_checklist"})
    _assert_checklist_answers(checklist, answers)

    distance = await _distance_to_relevant_contour(db, permit_id=permit_id, task=task, gps=gps)
    organization_id = await _resolve_act_organization(
        db, task=task, permit_id=permit_id, application_id=application_id, actor=actor
    )
    await _assert_organization_in_zone(db, actor, organization_id)

    act = InspectionAct(
        task_id=task_id,
        permit_id=permit_id,
        application_id=application_id,
        organization_id=organization_id,
        inspector_id=actor.id,
        occurred_at=occurred_at,
        gps=_gps_point(gps),
        gps_accuracy_m=gps_accuracy_m,
        distance_to_contour_m=distance,
        checklist_id=checklist_id,
        answers=answers,
        facts=facts,
        result=result,
        notes=notes,
        created_offline_at=created_offline_at,
        synced_at=datetime.now(UTC) if created_offline_at is not None else None,
    )
    db.add(act)
    await db.flush()
    if task is not None and task.status == "assigned":
        task.status = "in_progress"
        await db.flush()
    await audit.log(
        db,
        action=ACT_CREATE,
        user_id=actor.id,
        object_type="inspection_act",
        object_id=act.id,
        new_value={"task_id": str(task_id) if task_id else None, "result": result},
    )
    return act


async def _readable_act(db: AsyncSession, act_id: uuid.UUID, *, actor: User) -> InspectionAct:
    act = await repo.get_act(db, act_id)
    if act is None:
        raise err("ERR-SYS-003")
    if act.inspector_id == actor.id:
        return act
    if await _holds(db, actor, VIEW_ANY):
        await _assert_organization_in_zone(db, actor, act.organization_id)
        return act
    raise err("ERR-ACL-001")


async def get_act(db: AsyncSession, act_id: uuid.UUID, *, actor: User) -> InspectionAct:
    return await _readable_act(db, act_id, actor=actor)


async def act_card(db: AsyncSession, act_id: uuid.UUID, *, actor: User) -> dict[str, Any]:
    act = await _readable_act(db, act_id, actor=actor)
    act_files = await repo.list_act_files(db, act_id)
    gps_lonlat = None
    if act.gps is not None:
        lon, lat = await repo.act_gps_lonlat(db, act_id)
        if lon is not None and lat is not None:
            gps_lonlat = {"lon": lon, "lat": lat}
    return {"act": act, "files": act_files, "gps": gps_lonlat}


async def _act_scope(db: AsyncSession, actor: User) -> ColumnElement[bool]:
    if await _holds(db, actor, VIEW_ANY):
        return zone_filter(zone_of(actor), organization_col=InspectionAct.organization_id)
    return InspectionAct.inspector_id == actor.id


async def list_acts(
    db: AsyncSession, *, result: str | None, params: PageParams, actor: User
) -> tuple[Sequence[InspectionAct], int]:
    scope = await _act_scope(db, actor)
    return await repo.list_acts(db, scope=scope, result=result, params=params)


async def update_act(
    db: AsyncSession,
    act_id: uuid.UUID,
    *,
    occurred_at: datetime | None,
    gps: tuple[float, float] | None,
    gps_accuracy_m: Decimal | None,
    answers: dict[str, Any] | None,
    facts: dict[str, Any] | None,
    notes: str | None,
    result: str | None,
    actor: User,
) -> InspectionAct:
    act = await repo.get_act(db, act_id)
    if act is None:
        raise err("ERR-SYS-003")
    if act.inspector_id != actor.id:
        raise err("ERR-ACL-001")
    if act.status != "draft":
        raise err("ERR-INSP-001", details={"reason": "not_draft"})

    if occurred_at is not None:
        act.occurred_at = occurred_at
    if gps is not None:
        act.gps = _gps_point(gps)
        task = await repo.get_task(db, act.task_id) if act.task_id is not None else None
        act.distance_to_contour_m = await _distance_to_relevant_contour(
            db, permit_id=act.permit_id, task=task, gps=gps
        )
    if gps_accuracy_m is not None:
        act.gps_accuracy_m = gps_accuracy_m
    if answers is not None:
        act.answers = answers
    if facts is not None:
        act.facts = facts
    if notes is not None:
        act.notes = notes
    if result is not None:
        act.result = result

    checklist = await repo.get_checklist(db, act.checklist_id)
    if checklist is not None:
        _assert_checklist_answers(checklist, act.answers)

    await db.flush()
    await audit.log(
        db,
        action=ACT_UPDATE,
        user_id=actor.id,
        object_type="inspection_act",
        object_id=act.id,
        new_value={"result": act.result},
    )
    return act


async def attach_act_file(
    db: AsyncSession,
    act_id: uuid.UUID,
    *,
    file_id: uuid.UUID,
    kind: str,
    taken_at: datetime | None,
    gps: tuple[float, float] | None,
    device: dict[str, Any] | None,
    actor: User,
) -> InspectionActFile:
    """`POST /inspections/acts/{id}/files` — `file_id` names an already-
    uploaded row (`POST /files`, generic; this module invents no storage of
    its own). `MediaFile` read directly: a level-0 core model, the same
    reasoning `applications.service._own_document_file` states for itself."""
    act = await repo.get_act(db, act_id)
    if act is None:
        raise err("ERR-SYS-003")
    if act.inspector_id != actor.id:
        raise err("ERR-ACL-001")
    if act.status != "draft":
        raise err("ERR-INSP-001", details={"reason": "not_draft"})

    file = await db.get(MediaFile, file_id)
    if file is None or file.status != "active":
        raise err("ERR-VAL-001", details={"reason": "act_file_not_found"})
    if file.uploaded_by != actor.id:
        raise err("ERR-VAL-001", details={"reason": "act_file_not_owned"})
    if await repo.get_act_file(db, act_id, file_id) is not None:
        raise err("ERR-INSP-001", details={"reason": "already_attached"})

    if taken_at is not None or gps is not None or device is not None:
        await files.set_capture_metadata(db, file_id, taken_at=taken_at, gps=gps, device=device)

    link = InspectionActFile(act_id=act_id, file_id=file_id, kind=kind)
    db.add(link)
    await db.flush()
    await audit.log(
        db,
        action=ACT_ATTACH_FILE,
        user_id=actor.id,
        object_type="inspection_act",
        object_id=act.id,
        new_value={"file_id": str(file_id), "kind": kind},
    )
    return link


def _act_package_bytes(act: InspectionAct) -> bytes:
    """The bytes `sign()` hashes — a canonical snapshot of what the inspector
    is attesting to, sorted keys (the same shape `applications.service.
    _package_bytes` uses for its own signed package, ruling: the SHAPE must
    never change once acts exist to sign)."""
    payload = {
        "act_id": str(act.id),
        "inspector_id": str(act.inspector_id),
        "occurred_at": act.occurred_at.isoformat(),
        "checklist_id": str(act.checklist_id),
        "answers": act.answers,
        "facts": act.facts,
        "result": act.result,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")


async def _assert_violation_type(db: AsyncSession, item_id: uuid.UUID) -> ClassifierItem:
    item = await admin_repo.get_classifier_item(db, item_id)
    classifier = await admin_repo.get_classifier_by_code(db, VIOLATION_TYPES_CLASSIFIER_CODE)
    if (
        item is None
        or classifier is None
        or item.classifier_id != classifier.id
        or item.status != "active"
    ):
        raise err("ERR-VAL-001", details={"reason": "unknown_violation_type"})
    return item


async def sign_act(
    db: AsyncSession,
    act_id: uuid.UUID,
    *,
    pkcs7: str,
    violation_type_item_id: uuid.UUID | None,
    actor: User,
) -> InspectionAct:
    """`POST /inspections/acts/{id}/sign` — ruling 1 of the plan: the act is
    final only once ERI-signed (tz/04 С15: "чеклист → статус → подпись ЭРИ").
    `result="violation"` REQUIRES `violation_type_item_id` here, at the moment
    of finalizing, since neither the checklist nor the act schema itself
    names WHICH of VT-01…06 applies — the inspector classifies as they sign,
    not before."""
    act = await repo.get_act(db, act_id)
    if act is None:
        raise err("ERR-SYS-003")
    if act.inspector_id != actor.id:
        raise err("ERR-ACL-001")
    if act.status != "draft":
        raise err("ERR-INSP-001", details={"reason": "already_signed"})
    if act.result == "violation" and violation_type_item_id is None:
        raise err("ERR-VAL-001", details={"reason": "violation_type_required"})

    violation_type: ClassifierItem | None = None
    if act.result == "violation":
        assert violation_type_item_id is not None
        violation_type = await _assert_violation_type(db, violation_type_item_id)

    document = _act_package_bytes(act)
    await signatures_service.sign(
        db,
        object_type=OBJECT_TYPE_ACT,
        object_id=act.id,
        purpose=ACT_SIGN_PURPOSE,
        document=document,
        pkcs7=pkcs7,
        user=actor,
    )
    act.status = "signed"
    await db.flush()
    await audit.log(
        db,
        action=ACT_SIGN,
        user_id=actor.id,
        object_type="inspection_act",
        object_id=act.id,
        new_value={"result": act.result},
    )

    if act.task_id is not None:
        task = await repo.get_task(db, act.task_id)
        if task is not None:
            await _complete_task(db, task, actor=actor)

    if act.result == "violation":
        assert violation_type is not None
        await _open_case(db, act, violation_type=violation_type, actor=actor)

    return act


# --- violation cases ---------------------------------------------------------


async def _case_applicant_id(db: AsyncSession, act: InspectionAct) -> uuid.UUID | None:
    if act.application_id is not None:
        application = await applications_service.get(db, act.application_id)
        if application is not None:
            return application.applicant_id
    if act.permit_id is not None:
        permit = await permits_service.get(db, act.permit_id)
        if permit is not None:
            return permit.applicant_id
    return None


async def _open_case(
    db: AsyncSession, act: InspectionAct, *, violation_type: ClassifierItem, actor: User
) -> ViolationCase:
    """`result="violation"` on a SIGNED act auto-opens a case (design/02: "in
    code") — never a route of its own; `sign_act` is the only caller."""
    applicant_id = await _case_applicant_id(db, act)
    number = await next_public_number(db, NUMBER_PREFIX, business_today())
    case = ViolationCase(
        number=number,
        act_id=act.id,
        permit_id=act.permit_id,
        applicant_id=applicant_id,
        organization_id=act.organization_id,
        violation_type_item_id=violation_type.id,
        decision_due_at=add_working_days(business_today(), 10),
        status="opened",
    )
    db.add(case)
    await db.flush()
    db.add(
        ViolationCaseHistory(
            case_id=case.id, from_status=None, to_status="opened", changed_by=actor.id
        )
    )
    await db.flush()
    await audit.log(
        db,
        action=CASE_OPEN,
        user_id=actor.id,
        object_type="violation_case",
        object_id=case.id,
        new_value={"number": number, "violation_type": violation_type.code, "act_id": str(act.id)},
    )
    return case


async def _readable_case(db: AsyncSession, case_id: uuid.UUID, *, actor: User) -> ViolationCase:
    case = await repo.get_case(db, case_id)
    if case is None:
        raise err("ERR-SYS-003")
    if await _holds(db, actor, VIEW_ANY) or await _holds(db, actor, CASES_MANAGE):
        await _assert_organization_in_zone(db, actor, case.organization_id)
        return case
    act = await repo.get_act(db, case.act_id)
    if act is not None and act.inspector_id == actor.id:
        return case
    own_applicant = await auth_repo.get_own_applicant(db, actor.id)
    if own_applicant is not None and case.applicant_id == own_applicant.id:
        return case
    raise err("ERR-ACL-001")


async def get_case(db: AsyncSession, case_id: uuid.UUID, *, actor: User) -> ViolationCase:
    return await _readable_case(db, case_id, actor=actor)


async def case_card(db: AsyncSession, case_id: uuid.UUID, *, actor: User) -> dict[str, Any]:
    case = await _readable_case(db, case_id, actor=actor)
    history = await repo.list_case_history(db, case_id)
    appeals = await repo.list_appeals(db, case_id)
    return {"case": case, "history": history, "appeals": appeals}


async def _case_scope(db: AsyncSession, actor: User) -> ColumnElement[bool]:
    if await _holds(db, actor, VIEW_ANY) or await _holds(db, actor, CASES_MANAGE):
        return zone_filter(zone_of(actor), organization_col=ViolationCase.organization_id)
    own_applicant = await auth_repo.get_own_applicant(db, actor.id)
    if own_applicant is not None:
        return ViolationCase.applicant_id == own_applicant.id
    return ViolationCase.act_id.in_(
        select(InspectionAct.id).where(InspectionAct.inspector_id == actor.id)
    )


async def list_cases(
    db: AsyncSession, *, status: str | None, params: PageParams, actor: User
) -> tuple[Sequence[ViolationCase], int]:
    scope = await _case_scope(db, actor)
    return await repo.list_cases(db, scope=scope, status=status, params=params)


def _assert_case_transition(case: ViolationCase, to_status: str) -> None:
    if to_status not in CASE_TRANSITIONS.get(case.status, ()):
        raise err(
            "ERR-INSP-001",
            details={"reason": "bad_transition", "from": case.status, "to": to_status},
        )


async def _move_case(
    db: AsyncSession, case: ViolationCase, *, to_status: str, actor: User, note: str | None = None
) -> None:
    _assert_case_transition(case, to_status)
    db.add(
        ViolationCaseHistory(
            case_id=case.id,
            from_status=case.status,
            to_status=to_status,
            changed_by=actor.id,
            note=note,
        )
    )
    case.status = to_status
    await db.flush()


async def request_explanation(
    db: AsyncSession, case_id: uuid.UUID, *, actor: User
) -> ViolationCase:
    """`POST /inspections/cases/{id}/request-explanation` — tz/04 С16: 5
    working days to explain."""
    case = await repo.get_case(db, case_id)
    if case is None:
        raise err("ERR-SYS-003")
    if not await _holds(db, actor, CASES_MANAGE) and not await _holds(db, actor, ACTS_WRITE):
        raise err("ERR-ACL-001")
    await _assert_organization_in_zone(db, actor, case.organization_id)
    await _move_case(db, case, to_status="explanation_requested", actor=actor)
    case.explanation_due_at = add_working_days(business_today(), 5)
    await db.flush()
    await audit.log(
        db,
        action=CASE_REQUEST_EXPLANATION,
        user_id=actor.id,
        object_type="violation_case",
        object_id=case.id,
    )
    return case


async def submit_explanation(
    db: AsyncSession,
    case_id: uuid.UUID,
    *,
    text: str,
    file_id: uuid.UUID | None,
    actor: User,
) -> ViolationCase:
    """`POST /inspections/cases/{id}/explanation` — the violator's own
    submission (the applicant on their own case) or staff recording it on
    their behalf (`CASES_MANAGE`, e.g. collected on paper in the field)."""
    case = await repo.get_case(db, case_id)
    if case is None:
        raise err("ERR-SYS-003")
    own_applicant = await auth_repo.get_own_applicant(db, actor.id)
    is_own = own_applicant is not None and case.applicant_id == own_applicant.id
    if not is_own and not await _holds(db, actor, CASES_MANAGE):
        raise err("ERR-ACL-001")
    if file_id is not None:
        file = await db.get(MediaFile, file_id)
        if file is None or file.status != "active":
            raise err("ERR-VAL-001", details={"reason": "explanation_file_not_found"})
        # Whoever is calling (the violator themselves, or staff recording it
        # on their behalf) must be the one who uploaded the evidence — the
        # same ownership rule `_own_document_file` states for itself.
        if file.uploaded_by != actor.id:
            raise err("ERR-VAL-001", details={"reason": "explanation_file_not_owned"})

    await _move_case(db, case, to_status="explained", actor=actor)
    case.explanation_text = text
    case.explanation_file_id = file_id
    await db.flush()
    await audit.log(
        db,
        action=CASE_SUBMIT_EXPLANATION,
        user_id=actor.id,
        object_type="violation_case",
        object_id=case.id,
    )
    return case


async def decide_case(
    db: AsyncSession,
    case_id: uuid.UUID,
    *,
    decision: str,
    damage_amount: Decimal | None,
    damage_calc: dict[str, Any] | None,
    note: str | None,
    actor: User,
) -> ViolationCase:
    """`POST /inspections/cases/{id}/decide` — the raxbar's decision (tz/04
    С16, `CASES_MANAGE`): warning / suspend / revoke / transfer. **Recorded
    only** (plan ruling 2) — executing a `suspend`/`revoke` against the
    permit itself is a SEPARATE act on `permits`' own existing suspend/revoke
    routes, citing ground `PS-01` ("по результатам инспекции", migration
    `0023`); this module does not call `permits.decisions.decide` itself."""
    case = await repo.get_case(db, case_id)
    if case is None:
        raise err("ERR-SYS-003")
    if not await _holds(db, actor, CASES_MANAGE):
        raise err("ERR-ACL-001")
    await _assert_organization_in_zone(db, actor, case.organization_id)

    await _move_case(db, case, to_status="decided", actor=actor, note=note)
    case.decision = decision
    case.damage_amount = damage_amount
    case.damage_calc = damage_calc
    case.decided_by = actor.id
    case.decided_at = datetime.now(UTC)
    await db.flush()
    await audit.log(
        db,
        action=CASE_DECIDE,
        user_id=actor.id,
        object_type="violation_case",
        object_id=case.id,
        new_value={"decision": decision},
    )
    return case


async def appeal_case(
    db: AsyncSession, case_id: uuid.UUID, *, text: str, actor: User
) -> ViolationAppeal:
    """`POST /inspections/cases/{id}/appeal` — the violator's own appeal
    against a `decided` case (tz/04 С16: "Обжалование — отдельной записью")."""
    case = await repo.get_case(db, case_id)
    if case is None:
        raise err("ERR-SYS-003")
    own_applicant = await auth_repo.get_own_applicant(db, actor.id)
    if own_applicant is None or case.applicant_id != own_applicant.id:
        raise err("ERR-ACL-001")
    if await repo.get_open_appeal(db, case_id) is not None:
        raise err("ERR-INSP-001", details={"reason": "appeal_already_open"})

    await _move_case(db, case, to_status="appealed", actor=actor)
    appeal = ViolationAppeal(
        case_id=case_id, filed_by=actor.id, text=text, filed_at=datetime.now(UTC)
    )
    db.add(appeal)
    await db.flush()
    await audit.log(
        db, action=CASE_APPEAL, user_id=actor.id, object_type="violation_case", object_id=case.id
    )
    return appeal


async def resolve_appeal(
    db: AsyncSession, case_id: uuid.UUID, *, result: str, actor: User
) -> ViolationAppeal:
    """`POST /inspections/cases/{id}/appeal/resolve` — the raxbar's answer to
    the OPEN appeal; the case returns to `decided` (its decision may itself
    have changed via a fresh `decide_case` call, or stand as before — this
    route only closes the appeal, `decide_case` is the one place `decision`
    is ever written)."""
    case = await repo.get_case(db, case_id)
    if case is None:
        raise err("ERR-SYS-003")
    if not await _holds(db, actor, CASES_MANAGE):
        raise err("ERR-ACL-001")
    await _assert_organization_in_zone(db, actor, case.organization_id)
    appeal = await repo.get_open_appeal(db, case_id)
    if appeal is None:
        raise err("ERR-INSP-001", details={"reason": "no_open_appeal"})

    await _move_case(db, case, to_status="decided", actor=actor, note=result)
    appeal.result = result
    appeal.resolved_by = actor.id
    appeal.resolved_at = datetime.now(UTC)
    await db.flush()
    await audit.log(
        db,
        action=CASE_RESOLVE_APPEAL,
        user_id=actor.id,
        object_type="violation_case",
        object_id=case.id,
    )
    return appeal


async def close_case(db: AsyncSession, case_id: uuid.UUID, *, actor: User) -> ViolationCase:
    """`POST /inspections/cases/{id}/close` — tz/04 С16: "Закрытое дело →
    архив" (the ARCHIVE transition itself belongs to stage 4.7)."""
    case = await repo.get_case(db, case_id)
    if case is None:
        raise err("ERR-SYS-003")
    if not await _holds(db, actor, CASES_MANAGE):
        raise err("ERR-ACL-001")
    await _assert_organization_in_zone(db, actor, case.organization_id)
    await _move_case(db, case, to_status="closed", actor=actor)
    await audit.log(
        db, action=CASE_CLOSE, user_id=actor.id, object_type="violation_case", object_id=case.id
    )
    return case
