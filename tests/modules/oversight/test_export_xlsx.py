"""Stage 13: `GET /oversight/risk-indicators/export.xlsx` and `GET
/oversight/events/export.xlsx` are the two list routes on paper — same
filters, same zone scope, readable cells, the id last.

The shared test DB is persistent and never empty (lesson: "the test DB is
shared, persistent, and never empty"), so every test below scopes its
assertions to ROWS ITS OWN FIXTURE CREATED — a unique `object_id` for risk
indicators, a unique `event_type` for events — rather than to the table's
whole population."""

import uuid

import pytest

from app.core.events import Event
from app.modules.audit import service as audit
from app.modules.oversight import service
from app.modules.oversight.permissions import OVERSIGHT_VIEW
from tests.conftest import assert_export_cut, export_cap, xlsx_rows
from tests.modules.gis.conftest import _client_for

pytestmark = pytest.mark.asyncio

API = "/api/v1"
RISK = f"{API}/oversight/risk-indicators/export.xlsx"
EVENTS = f"{API}/oversight/events/export.xlsx"


async def _client_for_zoned(db, organization_id, *, grant: bool = True):
    permissions = (OVERSIGHT_VIEW,) if grant else ()
    async for client in _client_for(db, *permissions, organization_id=organization_id):
        yield client


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


async def test_the_risk_export_mirrors_the_list(db):
    object_id = uuid.uuid4()
    await _tag_risk_indicator(db, object_id=object_id)  # RI-12 is "high"
    await _tag_risk_indicator(db, object_id=uuid.uuid4())

    async for client in _client_for_zoned(db, None):
        listed = await client.get(
            f"{API}/oversight/risk-indicators", params={"object_id": str(object_id)}
        )
        assert listed.status_code == 200
        listed_ids = {item["id"] for item in listed.json()["items"]}
        assert listed_ids, "the fixture's own row must appear in the list"

        resp = await client.get(RISK, params={"object_id": str(object_id), "lang": "ru"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert resp.headers["x-export-truncated"] == "false"
        headers, rows = xlsx_rows(resp.content)
        assert headers[0] == "Код" and headers[-1] == "ID"
        assert {str(row[-1]) for row in rows} == listed_ids

        # Labels, not codes.
        resp = await client.get(RISK, params={"object_id": str(object_id), "lang": "uz_latn"})
        (row,) = xlsx_rows(resp.content)[1]
        assert row[0] == "RI-12"  # the domain code stays raw, first column
        assert row[1] == "yuqori"  # level label, not "high"
        assert row[2] == "Yangi"  # status label, not "new"
        assert row[3] == "Ariza"  # object_type label, not "application"
        assert row[4] == str(object_id)

        # The list's own `level` filter.
        resp = await client.get(
            RISK, params={"object_id": str(object_id), "level": "critical", "lang": "uz_latn"}
        )
        assert resp.status_code == 200
        assert xlsx_rows(resp.content)[1] == []

        with export_cap(1):  # two RI-12 rows tagged above, one fits
            resp = await client.get(RISK, params={"code": "RI-12"})
            assert int(resp.headers["x-export-total"]) > 1
            assert_export_cut(resp, cap=1)


async def test_risk_export_gets_the_same_403_the_list_gives_without_the_permission(db):
    async for client in _client_for_zoned(db, None, grant=False):
        listed = await client.get(f"{API}/oversight/risk-indicators")
        exported = await client.get(RISK)
        assert listed.status_code == exported.status_code == 403
        assert exported.json()["error"]["code"] == "ERR-ACL-001"


# --- events ---------------------------------------------------------------


def _event_type() -> str:
    return f"stage13-export-test-{uuid.uuid4().hex}"


async def test_the_events_export_mirrors_the_list(db):
    event_type = _event_type()
    application_id = uuid.uuid4()
    await service.record_event(
        db, Event(name=event_type, payload={"application_id": str(application_id)})
    )
    await service.record_event(
        db, Event(name=_event_type(), payload={"application_id": str(uuid.uuid4())})
    )

    async for client in _client_for_zoned(db, None):
        listed = await client.get(f"{API}/oversight/events", params={"event_type": event_type})
        assert listed.status_code == 200
        listed_ids = {item["id"] for item in listed.json()["items"]}
        assert listed_ids

        resp = await client.get(EVENTS, params={"event_type": event_type, "lang": "ru"})
        assert resp.status_code == 200
        assert resp.headers["x-export-truncated"] == "false"
        headers, rows = xlsx_rows(resp.content)
        assert headers[0] == "Событие" and headers[-1] == "ID"
        assert {str(row[-1]) for row in rows} == listed_ids

        # Labels, not codes.
        resp = await client.get(EVENTS, params={"event_type": event_type, "lang": "uz_latn"})
        (row,) = xlsx_rows(resp.content)[1]
        assert row[0] == event_type  # kept raw, an identifier not a vocabulary word
        assert row[1] == "Ariza"  # object_type label, not "application"
        assert row[5] == "Ichki"  # rn_status label, not "internal"

        # The list's own `object_type` filter.
        listed = await client.get(
            f"{API}/oversight/events", params={"event_type": event_type, "object_type": "permit"}
        )
        resp = await client.get(EVENTS, params={"event_type": event_type, "object_type": "permit"})
        assert listed.json()["items"] == []
        assert xlsx_rows(resp.content)[1] == []

        with export_cap(1):  # two events recorded above, one fits
            resp = await client.get(EVENTS)
            assert int(resp.headers["x-export-total"]) > 1
            assert_export_cut(resp, cap=1)


async def test_events_export_gets_the_same_403_the_list_gives_without_the_permission(db):
    async for client in _client_for_zoned(db, None, grant=False):
        listed = await client.get(f"{API}/oversight/events")
        exported = await client.get(EVENTS)
        assert listed.status_code == exported.status_code == 403
        assert exported.json()["error"]["code"] == "ERR-ACL-001"
