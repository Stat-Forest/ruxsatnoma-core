"""`POST /gis/imports` and `GET /gis/imports/{id}` — the upload half of ruling 6.

The endpoint stores and QUEUES; it parses nothing (`test_import_job.py` covers
what the job then does with the row). What it must get right is the gate:
gis's own MIME/magic table and size cap (ruling 8) instead of the document
whitelist `POST /files` uses, `CONTOURS_MANAGE`, and the zone.
"""

import json
import uuid
import zipfile

import numpy as np
import pytest
import shapely
from pyogrio.raw import write
from sqlalchemy import delete, select

from app.core import settings_store
from app.core.models import SystemSetting
from app.modules.audit.models import AuditLog
from app.modules.gis.models import GisImport
from tests.modules.gis.conftest import random_box_wkt

CAP_KEY = "gis_import_max_mb"


def shapefile_zip_bytes(tmp_path) -> bytes:
    """A real zipped shapefile — what the Agency actually delivers (ruling 5: a
    bare .shp is meaningless, the attributes live in the .dbf and the CRS in the
    .prj, so only the ZIP is accepted)."""
    polygon = shapely.from_wkt(random_box_wkt())
    write(
        str(tmp_path / "layer.shp"),
        geometry=shapely.to_wkb(np.array([polygon])),
        field_data=[np.array(["14520q"], dtype=object)],
        fields=np.array(["number"], dtype=object),
        geometry_type="Polygon",
        crs="EPSG:4326",
        driver="ESRI Shapefile",
        encoding="UTF-8",
    )
    archive_path = tmp_path / "layer.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in tmp_path.glob("layer.*"):
            if path.suffix != ".zip":
                archive.write(path, path.name)
    return archive_path.read_bytes()


def idem() -> dict[str, str]:
    """`POST /gis/imports` requires an Idempotency-Key (3.4's mechanism, first
    consumer). A FRESH key per call here: the client convention is that a retry
    reuses its key and a genuinely new request mints a new one, so a test that
    is not about replay must not accidentally replay."""
    return {"Idempotency-Key": str(uuid.uuid4())}


def form(*, org_id, doc_id, fmt="shp", layer_code="contours", attributes=None):
    return {
        "layer_code": layer_code,
        "organization_id": str(org_id),
        "approval_doc_id": str(doc_id),
        "format": fmt,
        "attributes": json.dumps({"number": "number"} if attributes is None else attributes),
    }


@pytest.fixture
async def _tiny_import_cap(db):
    """1 MB instead of 100 — proving the cap is wired without ever building a
    100 MB body in a test (lesson: "A cap checked after reading the body is not
    a cap" — the point is that it fires, cheaply)."""
    await db.execute(delete(SystemSetting).where(SystemSetting.key == CAP_KEY))
    db.add(SystemSetting(key=CAP_KEY, value=1))
    await db.commit()
    settings_store.invalidate(CAP_KEY)
    yield
    await db.execute(delete(SystemSetting).where(SystemSetting.key == CAP_KEY))
    await db.commit()
    settings_store.invalidate(CAP_KEY)


async def test_a_gis_specialist_queues_a_zipped_shapefile(
    gis_client, db, leshoz, approval_doc, tmp_path
):
    response = await gis_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=leshoz.id, doc_id=approval_doc.id),
        files={"file": ("burchmulla.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
        headers=idem(),
    )
    assert response.status_code == 202, response.text
    import_id = uuid.UUID(response.json()["import_id"])

    row = await db.get(GisImport, import_id)
    assert row is not None
    assert row.status == "pending"  # 202 means QUEUED, not parsed
    assert row.format == "shp"
    assert row.attribute_map == {"number": "number"}
    assert row.approval_doc_id == approval_doc.id
    audited = await db.execute(
        select(AuditLog).where(
            AuditLog.object_id == import_id, AuditLog.action == "gis_import.create"
        )
    )
    assert audited.scalars().first() is not None


async def test_the_status_route_reports_the_batch_back(
    gis_client, db, leshoz, approval_doc, tmp_path
):
    created = await gis_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=leshoz.id, doc_id=approval_doc.id),
        files={"file": ("burchmulla.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
        headers=idem(),
    )
    import_id = created.json()["import_id"]
    response = await gis_client.get(f"/api/v1/gis/imports/{import_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["stats"] is None and body["error_report"] is None
    assert body["organization_id"] == str(leshoz.id)


async def test_a_document_type_is_not_a_geodata_type(gis_client, leshoz, approval_doc, tmp_path):
    """Ruling 8's whole point: gis brings its OWN table. A PDF is perfectly
    acceptable to `POST /files` and must be refused here — and routing geodata
    through `/files` instead would have meant widening that whitelist for every
    uploader in the system."""
    response = await gis_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=leshoz.id, doc_id=approval_doc.id, fmt="zip"),
        files={"file": ("decree.pdf", b"%PDF-1.4 x", "application/pdf")},
        headers=idem(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "type_not_allowed"


async def test_bytes_that_do_not_match_the_declared_type_are_refused(
    gis_client, leshoz, approval_doc
):
    response = await gis_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=leshoz.id, doc_id=approval_doc.id, fmt="zip"),
        files={"file": ("layer.zip", b"not a zip at all", "application/zip")},
        headers=idem(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "content_mismatch"


async def test_a_body_over_the_cap_is_refused(gis_client, leshoz, approval_doc, _tiny_import_cap):
    response = await gis_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=leshoz.id, doc_id=approval_doc.id, fmt="zip"),
        files={"file": ("big.zip", b"PK\x03\x04" + b"\x00" * (2 * 1024 * 1024), "application/zip")},
        headers=idem(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "too_large"


async def test_an_unsupported_format_is_a_format_error(gis_client, leshoz, approval_doc, tmp_path):
    """RAR is the case that matters: the Agency's first delivery used one, and
    GDAL has no /vsirar (ruling 5)."""
    response = await gis_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=leshoz.id, doc_id=approval_doc.id, fmt="rar"),
        files={"file": ("burchmulla.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
        headers=idem(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR-GIS-004"


async def test_an_unknown_layer_is_a_404(gis_client, leshoz, approval_doc, tmp_path):
    response = await gis_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=leshoz.id, doc_id=approval_doc.id, layer_code="not_a_layer"),
        files={"file": ("burchmulla.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
        headers=idem(),
    )
    assert response.status_code == 404


async def test_an_approval_document_that_is_not_on_record_is_refused(gis_client, leshoz, tmp_path):
    """Ruling 3: one basis document per batch — and it has to exist before the
    batch does, since every version the import creates will carry it."""
    response = await gis_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=leshoz.id, doc_id=uuid.uuid4()),
        files={"file": ("burchmulla.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
        headers=idem(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "approval_doc_not_found"


async def test_a_malformed_attribute_map_is_rejected_at_the_request(
    gis_client, leshoz, approval_doc, tmp_path
):
    """`attributes` is a JSON string inside a multipart body. Garbage there must
    be a 422 on the request, not a 500 in the job twenty minutes later."""
    response = await gis_client.post(
        "/api/v1/gis/imports",
        data={
            **form(org_id=leshoz.id, doc_id=approval_doc.id),
            "attributes": "{not json",
        },
        files={"file": ("burchmulla.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
        headers=idem(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "attributes_not_json"


async def test_a_nested_attribute_map_is_rejected_too(gis_client, leshoz, approval_doc, tmp_path):
    """Valid JSON, wrong shape: the values are read back as FIELD NAMES, so a
    nested object would fail far from the request that supplied it."""
    response = await gis_client.post(
        "/api/v1/gis/imports",
        data={
            **form(org_id=leshoz.id, doc_id=approval_doc.id),
            "attributes": json.dumps({"number": {"field": "number"}}),
        },
        files={"file": ("burchmulla.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
        headers=idem(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "attributes_not_a_string_map"


async def test_a_leshoz_scoped_specialist_cannot_import_into_another_leshoz(
    org_scoped_gis_client, other_leshoz, approval_doc, tmp_path
):
    """Zone scoping is not a permission check (lesson): this actor holds
    CONTOURS_MANAGE and is still refused, because `organization_id` comes from
    the request body and names someone else's leshoz."""
    response = await org_scoped_gis_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=other_leshoz.id, doc_id=approval_doc.id),
        files={"file": ("burchmulla.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
        headers=idem(),
    )
    assert response.status_code == 403


async def test_an_approver_may_not_import(rahbar_client, leshoz, approval_doc, tmp_path):
    """`CONTOURS_APPROVE` is not `CONTOURS_MANAGE` — the rahbar approves the
    batch (Task 8), they do not file it."""
    response = await rahbar_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=leshoz.id, doc_id=approval_doc.id),
        files={"file": ("burchmulla.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
        headers=idem(),
    )
    assert response.status_code == 403


async def test_an_applicant_cannot_read_an_import_at_all(
    applicant_client, gis_client, db, leshoz, approval_doc, tmp_path
):
    created = await gis_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=leshoz.id, doc_id=approval_doc.id),
        files={"file": ("burchmulla.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
        headers=idem(),
    )
    import_id = created.json()["import_id"]
    response = await applicant_client.get(f"/api/v1/gis/imports/{import_id}")
    assert response.status_code == 403


async def test_an_unknown_import_is_a_404(gis_client):
    response = await gis_client.get(f"/api/v1/gis/imports/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_a_replayed_upload_returns_the_stored_202_and_queues_nothing_new(
    gis_client, db, leshoz, approval_doc, tmp_path
):
    """The reason `POST /gis/imports` carries an Idempotency-Key at all: a
    retried or double-clicked upload files a SECOND batch which then SUCCEEDS —
    151 `duplicate_number` warnings and a `/2` suffix on every contour — and
    there is no delete path, archiving being one contour at a time. The replay
    must come back with the ORIGINAL import_id and leave one row behind, not
    two. `auth.deps.idempotency_context` has shipped since 3.4 with no
    consumer; this is its first."""
    key = {"Idempotency-Key": str(uuid.uuid4())}
    payload = {
        "data": form(org_id=leshoz.id, doc_id=approval_doc.id),
        "files": {"file": ("burchmulla.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
    }

    first = await gis_client.post("/api/v1/gis/imports", **payload, headers=key)
    assert first.status_code == 202, first.text
    second = await gis_client.post("/api/v1/gis/imports", **payload, headers=key)
    assert second.status_code == 202, second.text
    assert second.json() == first.json()

    filed = await db.execute(select(GisImport.id).where(GisImport.organization_id == leshoz.id))
    assert [row for row in filed.scalars().all()] == [uuid.UUID(first.json()["import_id"])]


async def test_an_upload_without_an_idempotency_key_is_refused(
    gis_client, leshoz, approval_doc, tmp_path
):
    response = await gis_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=leshoz.id, doc_id=approval_doc.id),
        files={"file": ("burchmulla.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "idempotency_key_required"


async def test_a_different_upload_under_the_same_key_is_a_conflict(
    gis_client, leshoz, approval_doc, tmp_path
):
    """The fingerprint of a MULTIPART request is taken over the parsed form —
    FastAPI has already consumed the stream by the time a dependency runs, so
    `Request.body()` raises `RuntimeError("Stream consumed")` there (a 500,
    reproduced before the fix). Each file contributes its name, filename,
    content type and size, so a genuinely different upload under a reused key
    is still ERR-SYS-005 rather than a silent replay."""
    key = {"Idempotency-Key": str(uuid.uuid4())}
    first = await gis_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=leshoz.id, doc_id=approval_doc.id),
        files={"file": ("a.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
        headers=key,
    )
    assert first.status_code == 202, first.text
    second = await gis_client.post(
        "/api/v1/gis/imports",
        data=form(org_id=leshoz.id, doc_id=approval_doc.id),
        files={"file": ("a-different-name.zip", shapefile_zip_bytes(tmp_path), "application/zip")},
        headers=key,
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ERR-SYS-005"
