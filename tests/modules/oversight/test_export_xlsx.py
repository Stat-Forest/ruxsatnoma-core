"""Stage 13: `GET /oversight/risk-indicators/export.xlsx` and `GET
/oversight/events/export.xlsx` are the two list routes on paper — same
filters, same zone scope, readable cells, the id last.

The shared test DB is persistent and never empty (lesson: "the test DB is
shared, persistent, and never empty"), so every test below scopes its
assertions to ROWS ITS OWN FIXTURE CREATED — a unique `object_id` for risk
indicators, a unique `event_type` for events — rather than to the table's
whole population."""

import io
import uuid

import pytest
from openpyxl import load_workbook

from app.core import settings_store, xlsx
from app.core.events import Event
from app.modules.audit import service as audit
from app.modules.oversight import service
from app.modules.oversight.permissions import OVERSIGHT_VIEW
from tests.modules.gis.conftest import _client_for

pytestmark = pytest.mark.asyncio

API = "/api/v1"


async def _client_for_zoned(db, organization_id, *, grant: bool = True):
    permissions = (OVERSIGHT_VIEW,) if grant else ()
    async for client in _client_for(db, *permissions, organization_id=organization_id):
        yield client


def _sheet(content: bytes):
    sheet = load_workbook(io.BytesIO(content)).active
    assert sheet is not None
    return sheet


async def _tag_risk_indicator(db, *, object_id: uuid.UUID, code: str = "RI-12") -> None:
    """The same "honest stand-in" `tests/modules/oversight/test_harvest.py`
    uses: a bare `audit.log(..., extra={"risk_indicator": code})` is what
    every real module already does at the moment it detects one, and
    `service.harvest` is the one function that turns it into a
    `risk_indicators` row."""
    await audit.log(
        db,
        action="application.read",
        object_type="application",
        object_id=object_id,
        result="denied",
        basis="out_of_zone",
        extra={"risk_indicator": code},
    )
    await service.harvest(db)


# --- risk-indicators ----------------------------------------------------


async def test_risk_export_holds_exactly_the_rows_the_list_shows(db):
    object_id = uuid.uuid4()
    await _tag_risk_indicator(db, object_id=object_id)

    async for client in _client_for_zoned(db, None):
        listed = await client.get(
            f"{API}/oversight/risk-indicators", params={"object_id": str(object_id)}
        )
        assert listed.status_code == 200
        listed_ids = {item["id"] for item in listed.json()["items"]}
        assert listed_ids, "the fixture's own row must appear in the list"

        resp = await client.get(
            f"{API}/oversight/risk-indicators/export.xlsx",
            params={"object_id": str(object_id), "lang": "ru"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert resp.headers["x-export-truncated"] == "false"
        sheet = _sheet(resp.content)
        headers = [c.value for c in sheet[1]]
        assert headers[0] == "Код" and headers[-1] == "ID"
        exported_ids = {str(row[-1]) for row in sheet.iter_rows(min_row=2, values_only=True)}
        assert exported_ids == listed_ids


async def test_risk_export_applies_the_same_filters_as_the_list(db):
    object_id = uuid.uuid4()
    await _tag_risk_indicator(db, object_id=object_id)  # RI-12 is "high"

    async for client in _client_for_zoned(db, None):
        resp = await client.get(
            f"{API}/oversight/risk-indicators/export.xlsx",
            params={"object_id": str(object_id), "level": "critical", "lang": "uz_latn"},
        )
        assert resp.status_code == 200
        assert list(_sheet(resp.content).iter_rows(min_row=2, values_only=True)) == []


async def test_risk_export_renders_labels_not_codes(db):
    object_id = uuid.uuid4()
    await _tag_risk_indicator(db, object_id=object_id)

    async for client in _client_for_zoned(db, None):
        resp = await client.get(
            f"{API}/oversight/risk-indicators/export.xlsx",
            params={"object_id": str(object_id), "lang": "uz_latn"},
        )
        row = next(_sheet(resp.content).iter_rows(min_row=2, values_only=True))
        assert row[0] == "RI-12"  # the domain code stays raw, first column
        assert row[1] == "yuqori"  # level label, not "high"
        assert row[2] == "Yangi"  # status label, not "new"
        assert row[3] == "Ariza"  # object_type label, not "application"
        assert row[4] == str(object_id)


async def test_risk_export_truncates_at_the_cap_and_says_so(db, monkeypatch):
    await _tag_risk_indicator(db, object_id=uuid.uuid4())
    await _tag_risk_indicator(db, object_id=uuid.uuid4())

    real_get_int = settings_store.get_int

    async def one(db_, key):
        # `get_current_session` (auth) also reads `session_idle_minutes`
        # through this same function on every authenticated request — only
        # the export's OWN key is capped here, everything else answers for
        # real (lesson: a blanket monkeypatch of a shared reader breaks
        # every OTHER caller of it, not just the one under test).
        if key == xlsx.CAP_SETTING:
            return 1
        return await real_get_int(db_, key)

    monkeypatch.setattr(settings_store, "get_int", one)

    async for client in _client_for_zoned(db, None):
        resp = await client.get(
            f"{API}/oversight/risk-indicators/export.xlsx", params={"code": "RI-12"}
        )
        total = int(resp.headers["x-export-total"])
        assert total > 1
        assert resp.headers["x-export-truncated"] == "true"
        assert resp.headers["x-export-rows"] == "1"
        assert len(list(_sheet(resp.content).iter_rows(min_row=2, values_only=True))) <= 1


async def test_risk_export_gets_the_same_403_the_list_gives_without_the_permission(db):
    async for client in _client_for_zoned(db, None, grant=False):
        listed = await client.get(f"{API}/oversight/risk-indicators")
        exported = await client.get(f"{API}/oversight/risk-indicators/export.xlsx")
        assert listed.status_code == exported.status_code == 403
        assert exported.json()["error"]["code"] == "ERR-ACL-001"


async def test_risk_export_rejects_an_unknown_language(db):
    async for client in _client_for_zoned(db, None):
        resp = await client.get(
            f"{API}/oversight/risk-indicators/export.xlsx", params={"lang": "en"}
        )
        assert resp.status_code == 422


# --- events ---------------------------------------------------------------


def _event_type() -> str:
    return f"stage13-export-test-{uuid.uuid4().hex}"


async def test_events_export_holds_exactly_the_rows_the_list_shows(db):
    event_type = _event_type()
    application_id = uuid.uuid4()
    await service.record_event(
        db, Event(name=event_type, payload={"application_id": str(application_id)})
    )

    async for client in _client_for_zoned(db, None):
        listed = await client.get(f"{API}/oversight/events", params={"event_type": event_type})
        assert listed.status_code == 200
        listed_ids = {item["id"] for item in listed.json()["items"]}
        assert listed_ids

        resp = await client.get(
            f"{API}/oversight/events/export.xlsx",
            params={"event_type": event_type, "lang": "ru"},
        )
        assert resp.status_code == 200
        assert resp.headers["x-export-truncated"] == "false"
        sheet = _sheet(resp.content)
        headers = [c.value for c in sheet[1]]
        assert headers[0] == "Событие" and headers[-1] == "ID"
        exported_ids = {str(row[-1]) for row in sheet.iter_rows(min_row=2, values_only=True)}
        assert exported_ids == listed_ids


async def test_events_export_applies_the_same_filters_as_the_list(db):
    event_type = _event_type()
    await service.record_event(
        db, Event(name=event_type, payload={"application_id": str(uuid.uuid4())})
    )

    async for client in _client_for_zoned(db, None):
        listed = await client.get(
            f"{API}/oversight/events",
            params={"event_type": event_type, "object_type": "permit"},
        )
        resp = await client.get(
            f"{API}/oversight/events/export.xlsx",
            params={"event_type": event_type, "object_type": "permit"},
        )
        assert listed.json()["items"] == []
        assert list(_sheet(resp.content).iter_rows(min_row=2, values_only=True)) == []


async def test_events_export_renders_labels_not_codes(db):
    event_type = _event_type()
    await service.record_event(
        db, Event(name=event_type, payload={"application_id": str(uuid.uuid4())})
    )

    async for client in _client_for_zoned(db, None):
        resp = await client.get(
            f"{API}/oversight/events/export.xlsx",
            params={"event_type": event_type, "lang": "uz_latn"},
        )
        row = next(_sheet(resp.content).iter_rows(min_row=2, values_only=True))
        assert row[0] == event_type  # kept raw, an identifier not a vocabulary word
        assert row[1] == "Ariza"  # object_type label, not "application"
        assert row[5] == "Ichki"  # rn_status label, not "internal"


async def test_events_export_truncates_at_the_cap_and_says_so(db, monkeypatch):
    await service.record_event(
        db, Event(name=_event_type(), payload={"application_id": str(uuid.uuid4())})
    )
    await service.record_event(
        db, Event(name=_event_type(), payload={"application_id": str(uuid.uuid4())})
    )

    real_get_int = settings_store.get_int

    async def one(db_, key):
        if key == xlsx.CAP_SETTING:
            return 1
        return await real_get_int(db_, key)

    monkeypatch.setattr(settings_store, "get_int", one)

    async for client in _client_for_zoned(db, None):
        resp = await client.get(f"{API}/oversight/events/export.xlsx")
        total = int(resp.headers["x-export-total"])
        assert total > 1
        assert resp.headers["x-export-truncated"] == "true"
        assert resp.headers["x-export-rows"] == "1"
        assert len(list(_sheet(resp.content).iter_rows(min_row=2, values_only=True))) <= 1


async def test_events_export_gets_the_same_403_the_list_gives_without_the_permission(db):
    async for client in _client_for_zoned(db, None, grant=False):
        listed = await client.get(f"{API}/oversight/events")
        exported = await client.get(f"{API}/oversight/events/export.xlsx")
        assert listed.status_code == exported.status_code == 403
        assert exported.json()["error"]["code"] == "ERR-ACL-001"


async def test_events_export_rejects_an_unknown_language(db):
    async for client in _client_for_zoned(db, None):
        resp = await client.get(f"{API}/oversight/events/export.xlsx", params={"lang": "en"})
        assert resp.status_code == 422
