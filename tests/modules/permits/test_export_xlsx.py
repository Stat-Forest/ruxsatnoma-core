"""Stage 13: the permits export is the register on paper — same scope, same
filters, readable cells, the id last.

`other_zone_hodim_client` (zoned to `other_leshoz`) is the "same caller" of
shape 1: its zone hides `issued_permit` (in `leshoz`) exactly as the list
does, while `other_zone_issued_permit` (its own leshoz's) proves the export
is not simply empty.
"""

import pytest

from app.modules.permits.models import Permit
from app.modules.permits.service import _permit_number
from tests.conftest import assert_export_cut, export_cap, xlsx_rows

pytestmark = pytest.mark.asyncio

EXPORT = "/api/v1/permits/export.xlsx"


async def test_the_export_mirrors_the_list(
    hodim_client,
    other_zone_hodim_client,
    issued_permit: Permit,
    other_zone_issued_permit: Permit,
):
    """The zone the list applies, the filter it takes, labels rather than
    codes, and the cap — against the two permits the fixtures issued."""
    listed = (
        await other_zone_hodim_client.get("/api/v1/permits", params={"page_size": 100})
    ).json()
    listed_ids = {row["id"] for row in listed["items"]}
    assert str(issued_permit.id) not in listed_ids  # the zone hides the other leshoz's permit
    assert str(other_zone_issued_permit.id) in listed_ids

    resp = await other_zone_hodim_client.get(EXPORT, params={"lang": "ru"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.headers["x-export-truncated"] == "false"
    headers, rows = xlsx_rows(resp.content)
    assert headers[0] == "№ разрешения" and headers[-1] == "ID"
    assert {str(row[-1]) for row in rows} == listed_ids

    # `hodim_client` is nationwide (sees both fixture permits, plus whatever
    # this shared, persistent test DB already holds — lesson), so the filter
    # that proves narrowing has to pin an exact applicant, not assume "no
    # matches": `applicant_id` is fresh per fixture and picks out exactly
    # `issued_permit`. The same row shows the labels.
    resp = await hodim_client.get(
        EXPORT, params={"applicant_id": str(issued_permit.applicant_id), "lang": "uz_latn"}
    )
    assert resp.status_code == 200
    _, rows = xlsx_rows(resp.content)
    assert {str(row[-1]) for row in rows} == {str(issued_permit.id)}
    (row,) = rows
    assert row[0] == _permit_number(issued_permit.series, issued_permit.number)  # the number first
    assert row[1] == "Imzolar kutilmoqda"  # the status label, not "pending_signatures"

    with export_cap(1):  # two permits nationwide at least, one fits
        assert_export_cut(await hodim_client.get(EXPORT), cap=1)


async def test_export_is_empty_not_403_for_a_caller_with_no_scope(other_applicant_client):
    list_resp = await other_applicant_client.client.get("/api/v1/permits")
    assert list_resp.status_code == 200
    assert list_resp.json()["items"] == []

    resp = await other_applicant_client.client.get(EXPORT)
    assert resp.status_code == 200
    assert xlsx_rows(resp.content)[1] == []
