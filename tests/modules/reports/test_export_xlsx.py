"""Stage 13: `GET /reports/export.xlsx` and `GET /reports/forms/export.xlsx`
— the reports register and the form catalog on paper. Same zone, same
filters, readable cells, the id last. `GET /reports/{report_id}/export.xlsx`
(the per-report data export) is untouched and not tested here."""

import io

import httpx
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.reports.models import ReportForm
from tests.modules.reports.conftest import Signer


def _sheet(content: bytes):
    sheet = load_workbook(io.BytesIO(content)).active
    assert sheet is not None
    return sheet


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


async def test_reports_export_holds_exactly_the_rows_the_list_shows(
    hodim_client: httpx.AsyncClient,
    grazing_form: ReportForm,
    leshoz: Organization,
):
    report_id = await _create_report(
        hodim_client,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start="2027-01-01",
        period_end="2027-03-31",
    )

    listed = await hodim_client.get("/api/v1/reports", params={"page_size": 100})
    listed_ids = {row["id"] for row in listed.json()["items"]}
    assert report_id in listed_ids

    resp = await hodim_client.get("/api/v1/reports/export.xlsx", params={"lang": "ru"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    sheet = _sheet(resp.content)
    headers = [c.value for c in sheet[1]]
    assert headers[0] == "Форма" and headers[-1] == "ID"
    exported_ids = {str(row[-1]) for row in sheet.iter_rows(min_row=2, values_only=True)}
    assert exported_ids == listed_ids


async def test_reports_export_applies_the_same_filters_as_the_list(
    hodim_client: httpx.AsyncClient,
    grazing_form: ReportForm,
    leshoz: Organization,
):
    report_id = await _create_report(
        hodim_client,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start="2027-04-01",
        period_end="2027-06-30",
    )

    resp = await hodim_client.get(
        "/api/v1/reports/export.xlsx", params={"status": "approved", "lang": "uz_latn"}
    )
    assert resp.status_code == 200
    exported_ids = {
        str(row[-1]) for row in _sheet(resp.content).iter_rows(min_row=2, values_only=True)
    }
    assert report_id not in exported_ids  # the filter narrows to a status this report is not in


async def test_reports_export_renders_labels_not_codes(
    hodim_client: httpx.AsyncClient,
    grazing_form: ReportForm,
    leshoz: Organization,
):
    report_id = await _create_report(
        hodim_client,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start="2027-07-01",
        period_end="2027-09-30",
    )

    resp = await hodim_client.get("/api/v1/reports/export.xlsx", params={"lang": "uz_latn"})
    row = next(
        r for r in _sheet(resp.content).iter_rows(min_row=2, values_only=True) if r[-1] == report_id
    )
    assert row[5] == "Toʻldirilmoqda"  # the status LABEL, not "created"


async def test_reports_export_truncates_at_the_cap_and_says_so(
    hodim_client: httpx.AsyncClient,
    grazing_form: ReportForm,
    leshoz: Organization,
    monkeypatch,
):
    for period in (("2028-01-01", "2028-03-31"), ("2028-04-01", "2028-06-30")):
        await _create_report(
            hodim_client,
            form_id=grazing_form.id,
            organization_id=leshoz.id,
            period_start=period[0],
            period_end=period[1],
        )

    from app.core import settings_store

    real_get_int = settings_store.get_int

    async def capped(db_, key):
        if key == "register_export_max_rows":
            return 1
        return await real_get_int(db_, key)

    monkeypatch.setattr(settings_store, "get_int", capped)

    resp = await hodim_client.get("/api/v1/reports/export.xlsx")
    assert resp.status_code == 200
    total = int(resp.headers["x-export-total"])
    assert resp.headers["x-export-truncated"] == ("true" if total > 1 else "false")
    assert len(list(_sheet(resp.content).iter_rows(min_row=2, values_only=True))) <= 1
    assert resp.headers["x-export-rows"] == str(min(total, 1))


async def test_reports_export_requires_the_permission(viewer_client: httpx.AsyncClient):
    resp = await viewer_client.get("/api/v1/reports/export.xlsx")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"


async def test_reports_export_rejects_an_unknown_language(hodim_client: httpx.AsyncClient):
    resp = await hodim_client.get("/api/v1/reports/export.xlsx", params={"lang": "en"})
    assert resp.status_code == 422


# --- /reports/forms/export.xlsx --------------------------------------------


async def test_forms_export_holds_exactly_the_rows_the_list_shows(
    hodim_client: httpx.AsyncClient, grazing_form: ReportForm
):
    """`report_forms` is a GLOBAL, unscoped catalog (no zone), so — unlike
    every zone-scoped register in this fleet — its total can already exceed
    the list's own `page_size<=100` ceiling on this shared, persistent test
    DB (lesson: assert on something fresh, never assume an empty or small
    neighbourhood). Compared on TOTAL count (the list's own unpaged `total`,
    same query the export runs) plus this test's own fresh row's presence,
    not on full id-set equality against one capped list page."""
    listed = await hodim_client.get("/api/v1/reports/forms", params={"page_size": 1})
    listed_total = listed.json()["total"]

    resp = await hodim_client.get("/api/v1/reports/forms/export.xlsx", params={"lang": "ru"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert int(resp.headers["x-export-total"]) == listed_total
    sheet = _sheet(resp.content)
    headers = [c.value for c in sheet[1]]
    assert headers[0] == "Код" and headers[-1] == "ID"
    exported_ids = {str(row[-1]) for row in sheet.iter_rows(min_row=2, values_only=True)}
    assert str(grazing_form.id) in exported_ids


async def test_forms_export_is_not_swallowed_by_the_report_id_route(
    hodim_client: httpx.AsyncClient, grazing_form: ReportForm
):
    """The brief's own regression guard: `/reports/{report_id}` is a
    single-segment pattern and `/reports/forms/export.xlsx` is two segments
    past `/reports/`, so the two can never collide — but this pins it."""
    resp = await hodim_client.get("/api/v1/reports/forms/export.xlsx")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")


async def test_forms_export_applies_the_same_filter_as_the_list(
    hodim_client: httpx.AsyncClient, grazing_form: ReportForm
):
    resp = await hodim_client.get(
        "/api/v1/reports/forms/export.xlsx", params={"status": "draft", "lang": "uz_latn"}
    )
    assert resp.status_code == 200
    exported_ids = {
        str(row[-1]) for row in _sheet(resp.content).iter_rows(min_row=2, values_only=True)
    }
    assert str(grazing_form.id) not in exported_ids  # grazing_form is ACTIVE, not draft


async def test_forms_export_renders_labels_not_codes(
    hodim_client: httpx.AsyncClient, grazing_form: ReportForm
):
    resp = await hodim_client.get("/api/v1/reports/forms/export.xlsx", params={"lang": "uz_latn"})
    row = next(
        r
        for r in _sheet(resp.content).iter_rows(min_row=2, values_only=True)
        if r[-1] == str(grazing_form.id)
    )
    assert row[0] == grazing_form.code  # the human code first
    assert row[5] == "Faol"  # the status LABEL, not "active"


async def test_forms_export_truncates_at_the_cap_and_says_so(
    db: AsyncSession,
    hodim_client: httpx.AsyncClient,
    grazing_form: ReportForm,
    central_admin_signer: Signer,
    monkeypatch,
):
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

    from app.core import settings_store

    real_get_int = settings_store.get_int

    async def capped(db_, key):
        if key == "register_export_max_rows":
            return 1
        return await real_get_int(db_, key)

    monkeypatch.setattr(settings_store, "get_int", capped)

    resp = await hodim_client.get("/api/v1/reports/forms/export.xlsx")
    assert resp.status_code == 200
    total = int(resp.headers["x-export-total"])
    assert resp.headers["x-export-truncated"] == ("true" if total > 1 else "false")
    assert len(list(_sheet(resp.content).iter_rows(min_row=2, values_only=True))) <= 1
    assert resp.headers["x-export-rows"] == str(min(total, 1))


async def test_forms_export_requires_the_permission(viewer_client: httpx.AsyncClient):
    resp = await viewer_client.get("/api/v1/reports/forms/export.xlsx")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"


async def test_forms_export_rejects_an_unknown_language(hodim_client: httpx.AsyncClient):
    resp = await hodim_client.get("/api/v1/reports/forms/export.xlsx", params={"lang": "en"})
    assert resp.status_code == 422
