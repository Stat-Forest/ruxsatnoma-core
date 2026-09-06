"""HTTP-level coverage: permission gates, and one full round trip through the
real routes (form creation -> report creation -> generate -> submit -> sign
-> approve -> export). Everything else about the lifecycle is covered more
cheaply at the service layer (`test_service.py`)."""

import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.reports import repo, service
from tests.modules.reports.conftest import Signer, sign_report_request


async def test_list_reports_requires_reports_view(viewer_client: httpx.AsyncClient):
    result = await viewer_client.get("/api/v1/reports")
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "ERR-ACL-001"


async def test_create_report_requires_reports_manage(
    viewer_client: httpx.AsyncClient, leshoz: Organization
):
    result = await viewer_client.post(
        "/api/v1/reports",
        json={
            "form_id": str(uuid.uuid4()),
            "organization_id": str(leshoz.id),
            "period_start": "2027-01-01",
            "period_end": "2027-03-31",
        },
    )
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "ERR-ACL-001"


async def test_create_form_requires_reports_forms_manage(hodim_client: httpx.AsyncClient):
    """`reports.manage` (the hodim's grant) is not `reports.forms.manage` —
    building the form catalog is central-office-only (tz/03's matrix)."""
    result = await hodim_client.post(
        "/api/v1/reports/forms",
        json={
            "code": "x",
            "version": 1,
            "name": {"uz_cyrl": "x"},
            "period_type": "month",
            "columns": [
                {
                    "code": "a",
                    "label": {"uz_cyrl": "A"},
                    "source": "auto",
                    "type": "text",
                }
            ],
        },
    )
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "ERR-ACL-001"


async def test_full_round_trip(
    db: AsyncSession,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
    hodim_client: httpx.AsyncClient,
    head_signer: Signer,
    central_admin_signer: Signer,
):
    form_code = f"2-ilova-http-{uuid.uuid4().hex[:8]}"
    created = await central_admin_signer.client.post(
        "/api/v1/reports/forms",
        json={
            "code": form_code,
            "version": 1,
            "name": {"uz_cyrl": "2-илова", "uz_latn": "2-ilova"},
            "activity_type_id": str(grazing_activity_id),
            "period_type": "quarter",
            "columns": [
                {
                    "code": "total_amount",
                    "label": {"uz_cyrl": "Сумма", "uz_latn": "Summa"},
                    "source": "auto",
                    "type": "money",
                },
                {
                    "code": "paid_amount",
                    "label": {"uz_cyrl": "Тўланган", "uz_latn": "Toʻlangan"},
                    "source": "auto",
                    "type": "money",
                },
            ],
        },
    )
    assert created.status_code == 201, created.text
    form_id = created.json()["id"]

    activated = await central_admin_signer.client.post(f"/api/v1/reports/forms/{form_id}/activate")
    assert activated.status_code == 200, activated.text

    created_report = await hodim_client.post(
        "/api/v1/reports",
        json={
            "form_id": form_id,
            "organization_id": str(leshoz.id),
            "period_start": "2029-01-01",
            "period_end": "2029-03-31",
        },
    )
    assert created_report.status_code == 201, created_report.text
    report_id = created_report.json()["id"]
    assert created_report.json()["status"] == "created"

    generated = await hodim_client.post(f"/api/v1/reports/{report_id}/generate")
    assert generated.status_code == 200, generated.text
    assert generated.json()["data"]["rows"] == []

    submitted = await hodim_client.post(f"/api/v1/reports/{report_id}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "submitted"

    # The bytes the rahbar's ERI must cover are `service._report_bytes` over
    # the STORED row — not something a client recomputes from the JSON
    # response (that function is not part of the public API) — so this test
    # reads the row straight from the database, the way a real E-IMZO client
    # would build its own signed envelope against the server-canonical form.
    report_row = await repo.get_report(db, uuid.UUID(report_id))
    assert report_row is not None
    document = service._report_bytes(report_row)

    signed = await sign_report_request(head_signer, uuid.UUID(report_id), document)
    assert signed.status_code == 200, signed.text
    assert signed.json()["status"] == "head_approved"

    approved = await central_admin_signer.client.post(f"/api/v1/reports/{report_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    xlsx = await hodim_client.get(f"/api/v1/reports/{report_id}/export.xlsx")
    assert xlsx.status_code == 200, xlsx.text
    assert xlsx.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert len(xlsx.content) > 0

    pdf = await hodim_client.get(f"/api/v1/reports/{report_id}/export.pdf")
    assert pdf.status_code == 200, pdf.text
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")


async def test_return_requires_sign_or_accept_permission(
    viewer_client: httpx.AsyncClient, leshoz: Organization
):
    result = await viewer_client.post(
        f"/api/v1/reports/{uuid.uuid4()}/return", json={"comment": "x"}
    )
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "ERR-ACL-001"
