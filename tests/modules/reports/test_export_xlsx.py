"""Stage 13: `GET /reports/export.xlsx` and `GET /reports/forms/export.xlsx`
— the reports register and the form catalog on paper. Same zone, same
filters, readable cells, the id last. `GET /reports/{report_id}/export.xlsx`
(the per-report data export) is untouched and not tested here."""

import httpx

from app.modules.admin.models import Organization
from app.modules.reports.models import ReportForm
from tests.conftest import assert_export_cut, export_cap, xlsx_rows
from tests.modules.reports.conftest import Signer

REPORTS = "/api/v1/reports/export.xlsx"
FORMS = "/api/v1/reports/forms/export.xlsx"


async def _create_report(
    client: httpx.AsyncClient, *, form_id, organization_id, period_start, period_end
):
    resp = await client.post(
        "/api/v1/reports",
        json={
            "form_id": str(form_id),
            "organization_id": str(organization_id),
            "period_start": period_start,
            "period_end": period_end,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# --- /reports/export.xlsx ---------------------------------------------------


async def test_the_reports_export_mirrors_the_list(
    hodim_client: httpx.AsyncClient,
    grazing_form: ReportForm,
    leshoz: Organization,
):
    """The same rows as the list, the list's status filter, the status as a
    label, and the cap — two reports created through the real route."""
    report_id = await _create_report(
        hodim_client,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start="2027-01-01",
        period_end="2027-03-31",
    )
    await _create_report(
        hodim_client,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start="2027-04-01",
        period_end="2027-06-30",
    )

    listed = await hodim_client.get("/api/v1/reports", params={"page_size": 100})
    listed_ids = {row["id"] for row in listed.json()["items"]}
    assert report_id in listed_ids

    resp = await hodim_client.get(REPORTS, params={"lang": "ru"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Форма" and headers[-1] == "ID"
    assert {str(row[-1]) for row in rows} == listed_ids

    _, rows = xlsx_rows((await hodim_client.get(REPORTS, params={"lang": "uz_latn"})).content)
    row = next(r for r in rows if r[-1] == report_id)
    assert row[5] == "Toʻldirilmoqda"  # the status LABEL, not "created"

    resp = await hodim_client.get(REPORTS, params={"status": "approved", "lang": "uz_latn"})
    assert resp.status_code == 200
    # The filter narrows to a status this report is not in.
    assert report_id not in {str(row[-1]) for row in xlsx_rows(resp.content)[1]}

    with export_cap(1):
        assert_export_cut(await hodim_client.get(REPORTS), cap=1)


async def test_reports_export_requires_the_permission(viewer_client: httpx.AsyncClient):
    resp = await viewer_client.get(REPORTS)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"


# --- /reports/forms/export.xlsx --------------------------------------------


async def test_the_forms_export_mirrors_the_list(
    hodim_client: httpx.AsyncClient, grazing_form: ReportForm, central_admin_signer: Signer
):
    """`report_forms` is a GLOBAL, unscoped catalog (no zone), so — unlike
    every zone-scoped register in this fleet — its total can already exceed
    the list's own `page_size<=100` ceiling on this shared, persistent test
    DB (lesson: assert on something fresh, never assume an empty or small
    neighbourhood). Compared on TOTAL count (the list's own unpaged `total`,
    same query the export runs) plus this test's own fresh row's presence,
    not on full id-set equality against one capped list page.

    Also the brief's own regression guard: `/reports/{report_id}` is a
    single-segment pattern and `/reports/forms/export.xlsx` is two segments
    past `/reports/`, so the two can never collide — the 200 pins it."""
    another = await central_admin_signer.client.post(
        "/api/v1/reports/forms",
        json={
            "code": f"3-ilova-{grazing_form.id.hex[:8]}",
            "version": 1,
            "name": {"uz_cyrl": "3-илова", "uz_latn": "3-ilova"},
            "period_type": "month",
            "columns": [
                {
                    "code": "a",
                    "label": {"uz_cyrl": "A", "uz_latn": "A"},
                    "source": "manual",
                    "type": "text",
                }
            ],
        },
    )
    assert another.status_code == 201, another.text

    listed = await hodim_client.get("/api/v1/reports/forms", params={"page_size": 1})
    listed_total = listed.json()["total"]

    resp = await hodim_client.get(FORMS, params={"lang": "ru"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert int(resp.headers["x-export-total"]) == listed_total
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "Код" and headers[-1] == "ID"
    assert str(grazing_form.id) in {str(row[-1]) for row in rows}

    _, rows = xlsx_rows((await hodim_client.get(FORMS, params={"lang": "uz_latn"})).content)
    row = next(r for r in rows if r[-1] == str(grazing_form.id))
    assert row[0] == grazing_form.code  # the human code first
    assert row[5] == "Faol"  # the status LABEL, not "active"

    resp = await hodim_client.get(FORMS, params={"status": "draft", "lang": "uz_latn"})
    assert resp.status_code == 200
    # grazing_form is ACTIVE, not draft.
    assert str(grazing_form.id) not in {str(row[-1]) for row in xlsx_rows(resp.content)[1]}

    with export_cap(1):  # `grazing_form` and `another` at least, one fits
        assert_export_cut(await hodim_client.get(FORMS), cap=1)


async def test_forms_export_requires_the_permission(viewer_client: httpx.AsyncClient):
    resp = await viewer_client.get(FORMS)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"
