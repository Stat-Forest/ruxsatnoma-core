"""Stage 13: `/inspections/{tasks,acts,cases}/export.xlsx` are their lists on
paper — same scope, same filters, readable cells, the id last."""

import uuid

import pytest

from app.modules.applications.models import Application
from app.modules.inspections import repo as inspections_repo
from app.modules.inspections import service as inspections_service
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from tests.conftest import assert_export_cut, export_cap, xlsx_rows

pytestmark = pytest.mark.asyncio

API = "/api/v1/inspections"
TASKS = f"{API}/tasks/export.xlsx"
ACTS = f"{API}/acts/export.xlsx"
CASES = f"{API}/cases/export.xlsx"


def _ids(rows) -> set[str]:
    """The last column of every data row, as strings — a cell is typed as a
    broad union openpyxl itself does not guarantee hashable, so every id is
    coerced through `str()` before it enters a set (every id column ever
    holds one already, via `xlsx.id_column`)."""
    return {str(row[-1]) for row in rows}


def _row(rows, id_: str):
    return next(r for r in rows if str(r[-1]) == id_)


async def _create_task(client, application: Application, inspector) -> str:
    r = await client.post(
        f"{API}/tasks",
        json={
            "kind": "permit_inspection",
            "application_id": str(application.id),
            "assigned_to": str(inspector.id),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_act(
    client, application: Application, checklist_id, *, result: str = "compliant"
) -> str:
    answers = {"activity_matches": True, "within_contour": result != "violation"}
    r = await client.post(
        f"{API}/acts",
        json={
            "application_id": str(application.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(checklist_id),
            "answers": answers,
            "result": result,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _open_case(db, inspector_client, inspector, application, checklist_id, vt_id) -> str:
    """A signed `violation` act opens a case automatically (`service.sign_act`)
    — never assigned by hand (lesson: build a fixture's precondition through
    the real transition)."""
    act_id = await _create_act(inspector_client, application, checklist_id, result="violation")
    act = await inspections_repo.get_act(db, uuid.UUID(act_id))
    assert act is not None
    pkcs7 = encode_mock_signature(
        document=inspections_service._act_package_bytes(act),
        serial=f"SN-{inspector.pinfl}",
        issuer="ISS-1",
        pinfl=inspector.pinfl,
    )
    signed = await inspector_client.post(
        f"{API}/acts/{act_id}/sign", json={"pkcs7": pkcs7, "violation_type_item_id": str(vt_id)}
    )
    assert signed.status_code == 200, signed.text
    case = await inspections_repo.case_for_act(db, act.id)
    assert case is not None
    return str(case.id)


# --- Tasks -----------------------------------------------------------------


async def test_the_tasks_export_mirrors_the_list(
    executor_head_client, application: Application, inspector
) -> None:
    task_id = await _create_task(executor_head_client, application, inspector)

    listed = (await executor_head_client.get(f"{API}/tasks", params={"page_size": 100})).json()
    listed_ids = {row["id"] for row in listed["items"]}
    assert task_id in listed_ids

    resp = await executor_head_client.get(TASKS, params={"lang": "ru"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.headers["x-export-truncated"] == "false"
    headers, rows = xlsx_rows(resp.content)
    assert headers[-1] == "ID"
    assert _ids(rows) == listed_ids

    _, rows = xlsx_rows((await executor_head_client.get(TASKS, params={"lang": "uz_latn"})).content)
    row = _row(rows, task_id)
    assert row[0] == "Ruxsatnomani tekshirish"  # kind label, not "permit_inspection"
    assert row[1] == "Tayinlangan"  # status label, not "assigned"

    resp = await executor_head_client.get(TASKS, params={"status": "cancelled", "lang": "uz_latn"})
    assert resp.status_code == 200
    assert xlsx_rows(resp.content)[1] == []

    with export_cap(1):
        assert_export_cut(await executor_head_client.get(TASKS), cap=1)


async def test_tasks_export_is_empty_not_403_for_a_caller_with_no_scope(
    other_inspector_client,
) -> None:
    resp = await other_inspector_client.get(TASKS)
    assert resp.status_code == 200
    assert xlsx_rows(resp.content)[1] == []


# --- Acts --------------------------------------------------------------


async def test_the_acts_export_mirrors_the_list(
    inspector_client, application: Application, default_checklist_id
) -> None:
    act_id = await _create_act(inspector_client, application, default_checklist_id)
    warning_id = await _create_act(
        inspector_client, application, default_checklist_id, result="warning"
    )

    listed = (await inspector_client.get(f"{API}/acts", params={"page_size": 100})).json()
    listed_ids = {row["id"] for row in listed["items"]}
    assert act_id in listed_ids

    resp = await inspector_client.get(ACTS, params={"lang": "ru"})
    assert resp.status_code == 200
    assert resp.headers["x-export-truncated"] == "false"
    headers, rows = xlsx_rows(resp.content)
    assert headers[-1] == "ID"
    assert _ids(rows) == listed_ids

    _, rows = xlsx_rows((await inspector_client.get(ACTS, params={"lang": "uz_latn"})).content)
    row = _row(rows, warning_id)
    assert row[1] == "Qoralama"  # status label ("draft"), not the raw code
    assert row[2] == "Eslatma"  # result label ("warning"), not the raw code

    resp = await inspector_client.get(ACTS, params={"result": "violation", "lang": "uz_latn"})
    assert resp.status_code == 200
    assert xlsx_rows(resp.content)[1] == []

    with export_cap(1):  # two acts of this inspector's, one fits
        resp = await inspector_client.get(ACTS)
        assert int(resp.headers["x-export-total"]) >= 2
        assert_export_cut(resp, cap=1)


async def test_acts_export_is_empty_not_403_for_a_caller_with_no_scope(
    other_inspector_client,
) -> None:
    resp = await other_inspector_client.get(ACTS)
    assert resp.status_code == 200
    assert xlsx_rows(resp.content)[1] == []


# --- Cases -----------------------------------------------------------------


async def test_the_cases_export_mirrors_the_list(
    db,
    executor_head_client,
    inspector_client,
    inspector,
    application: Application,
    default_checklist_id,
    vt_01,
) -> None:
    case_id = await _open_case(
        db, inspector_client, inspector, application, default_checklist_id, vt_01
    )

    listed = (await executor_head_client.get(f"{API}/cases", params={"page_size": 100})).json()
    listed_ids = {row["id"] for row in listed["items"]}
    assert case_id in listed_ids

    resp = await executor_head_client.get(CASES, params={"lang": "ru"})
    assert resp.status_code == 200
    assert resp.headers["x-export-truncated"] == "false"
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Номер дела" and headers[-1] == "ID"
    assert _ids(rows) == listed_ids

    _, rows = xlsx_rows((await executor_head_client.get(CASES, params={"lang": "uz_latn"})).content)
    row = _row(rows, case_id)
    assert row[1] == "Ochilgan"  # status label ("opened"), not the raw code
    # the human case number comes first (service.NUMBER_PREFIX)
    assert isinstance(row[0], str) and row[0].startswith("VC-")

    resp = await executor_head_client.get(CASES, params={"status": "closed", "lang": "uz_latn"})
    assert resp.status_code == 200
    assert xlsx_rows(resp.content)[1] == []

    with export_cap(1):
        assert_export_cut(await executor_head_client.get(CASES), cap=1)


async def test_cases_export_is_empty_not_403_for_a_caller_with_no_scope(
    other_inspector_client,
) -> None:
    resp = await other_inspector_client.get(CASES)
    assert resp.status_code == 200
    assert xlsx_rows(resp.content)[1] == []
