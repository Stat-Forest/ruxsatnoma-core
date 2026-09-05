"""`POST /inspections/acts` and its lifecycle (tz/04 С15) — checklist
validation, GPS/distance, photo/video attachments, and the ERI signature that
finalizes the act (`ruling 1`)."""

import io
import uuid

from app.modules.applications.models import Application
from app.modules.inspections import repo, service
from app.modules.integrations.adapters.eimzo import encode_mock_signature

API = "/api/v1/inspections"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


def _answers(*, activity_matches: bool = True, within_contour: bool = True) -> dict:
    return {"activity_matches": activity_matches, "within_contour": within_contour}


async def test_inspector_creates_a_draft_act(
    inspector_client, application: Application, default_checklist_id: uuid.UUID
) -> None:
    r = await inspector_client.post(
        f"{API}/acts",
        json={
            "application_id": str(application.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(default_checklist_id),
            "answers": _answers(),
            "facts": {"head_count": 12},
            "result": "compliant",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft"
    assert body["inspector_id"] is not None


async def test_missing_required_answer_is_refused(
    inspector_client, application: Application, default_checklist_id: uuid.UUID
) -> None:
    r = await inspector_client.post(
        f"{API}/acts",
        json={
            "application_id": str(application.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(default_checklist_id),
            "answers": {"activity_matches": True},  # within_contour missing
            "result": "compliant",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ERR-INSP-002"
    assert "within_contour" in r.json()["error"]["details"]["missing"]


async def test_an_act_needs_a_subject_or_a_location(
    inspector_client, default_checklist_id: uuid.UUID
) -> None:
    r = await inspector_client.post(
        f"{API}/acts",
        json={
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(default_checklist_id),
            "answers": _answers(),
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ERR-VAL-001"


async def test_gps_computes_distance_to_the_published_contour(
    inspector_client, application: Application, default_checklist_id: uuid.UUID
) -> None:
    """The GPS fix is intentionally far from `application`'s own contour (a
    random box somewhere in [0,40]x[0,30]) — the point (200, 80) is outside
    WGS84 range for a REAL fix but PostGIS still computes a geodesic distance
    over it, which is all this test needs: a large, non-null number."""
    r = await inspector_client.post(
        f"{API}/acts",
        json={
            "application_id": str(application.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "gps": {"lon": 69.2401, "lat": 41.2995},
            "checklist_id": str(default_checklist_id),
            "answers": _answers(),
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["distance_to_contour_m"] is not None


async def test_only_the_owning_inspector_may_update_a_draft_act(
    inspector_client,
    other_inspector_client,
    application: Application,
    default_checklist_id: uuid.UUID,
) -> None:
    created = await inspector_client.post(
        f"{API}/acts",
        json={
            "application_id": str(application.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(default_checklist_id),
            "answers": _answers(),
        },
    )
    act_id = created.json()["id"]

    r = await other_inspector_client.patch(f"{API}/acts/{act_id}", json={"notes": "hijacked"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"

    ok = await inspector_client.patch(f"{API}/acts/{act_id}", json={"notes": "own note"})
    assert ok.status_code == 200
    assert ok.json()["notes"] == "own note"


async def test_attach_photo_then_sign_finalizes_the_act(
    db, inspector, inspector_client, application: Application, default_checklist_id: uuid.UUID
) -> None:
    created = await inspector_client.post(
        f"{API}/acts",
        json={
            "application_id": str(application.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(default_checklist_id),
            "answers": _answers(),
            "result": "compliant",
        },
    )
    act_id = created.json()["id"]

    uploaded = await inspector_client.post(
        "/api/v1/files", files={"file": ("photo.png", io.BytesIO(PNG), "image/png")}
    )
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["id"]

    attached = await inspector_client.post(
        f"{API}/acts/{act_id}/files",
        json={
            "file_id": file_id,
            "kind": "photo",
            "gps": {"lon": 69.2401, "lat": 41.2995},
            "device": {"model": "Pixel 8"},
        },
    )
    assert attached.status_code == 201, attached.text

    act = await repo.get_act(db, uuid.UUID(act_id))
    assert act is not None
    document = service._act_package_bytes(act)
    pkcs7 = encode_mock_signature(
        document=document, serial=f"SN-{inspector.pinfl}", issuer="ISS-1", pinfl=inspector.pinfl
    )
    signed = await inspector_client.post(f"{API}/acts/{act_id}/sign", json={"pkcs7": pkcs7})
    assert signed.status_code == 200, signed.text
    assert signed.json()["status"] == "signed"

    # A signed act cannot be re-signed.
    again = await inspector_client.post(f"{API}/acts/{act_id}/sign", json={"pkcs7": pkcs7})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ERR-INSP-001"

    # ...nor updated.
    locked = await inspector_client.patch(f"{API}/acts/{act_id}", json={"notes": "too late"})
    assert locked.status_code == 409


async def test_signing_a_violation_result_requires_a_violation_type(
    db, inspector, inspector_client, application: Application, default_checklist_id: uuid.UUID
) -> None:
    created = await inspector_client.post(
        f"{API}/acts",
        json={
            "application_id": str(application.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(default_checklist_id),
            "answers": _answers(within_contour=False),
            "result": "violation",
        },
    )
    act_id = created.json()["id"]
    act = await repo.get_act(db, uuid.UUID(act_id))
    assert act is not None
    document = service._act_package_bytes(act)
    pkcs7 = encode_mock_signature(
        document=document, serial=f"SN-{inspector.pinfl}", issuer="ISS-1", pinfl=inspector.pinfl
    )

    without_type = await inspector_client.post(f"{API}/acts/{act_id}/sign", json={"pkcs7": pkcs7})
    assert without_type.status_code == 422
    assert without_type.json()["error"]["code"] == "ERR-VAL-001"


async def test_completing_a_task_s_act_marks_the_task_done(
    db,
    inspector,
    inspector_client,
    executor_head_client,
    application: Application,
    default_checklist_id: uuid.UUID,
) -> None:
    task = await executor_head_client.post(
        f"{API}/tasks",
        json={
            "kind": "permit_inspection",
            "application_id": str(application.id),
            "assigned_to": str(inspector.id),
        },
    )
    task_id = task.json()["id"]

    created = await inspector_client.post(
        f"{API}/acts",
        json={
            "task_id": task_id,
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(default_checklist_id),
            "answers": _answers(),
            "result": "compliant",
        },
    )
    act_id = created.json()["id"]
    act = await repo.get_act(db, uuid.UUID(act_id))
    assert act is not None
    pkcs7 = encode_mock_signature(
        document=service._act_package_bytes(act),
        serial=f"SN-{inspector.pinfl}",
        issuer="ISS-1",
        pinfl=inspector.pinfl,
    )
    signed = await inspector_client.post(f"{API}/acts/{act_id}/sign", json={"pkcs7": pkcs7})
    assert signed.status_code == 200, signed.text

    task_after = await inspector_client.get(f"{API}/tasks/{task_id}")
    assert task_after.json()["status"] == "done"
    assert task_after.json()["completed_at"] is not None
