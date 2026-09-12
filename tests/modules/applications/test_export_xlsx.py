"""Stage 13 (ruling #204): `GET /applications/export.xlsx` is the list on paper —
the same scope, the same filters, readable cells, the id last.

Every shape here compares the FILE against the LIST for the same caller,
because the one defect this route could carry that matters is a disagreement
between the two (ruling #98: an export that widens a zone; or, the direction
this project's defects actually take, one that silently narrows it).
"""

import io
import uuid

import pytest
from openpyxl import load_workbook

from app.core import settings_store

pytestmark = pytest.mark.asyncio

EXPORT = "/api/v1/applications/export.xlsx"


def _sheet(content: bytes):
    sheet = load_workbook(io.BytesIO(content)).active
    assert sheet is not None
    return sheet


def _data_rows(content: bytes) -> list[tuple]:
    return list(_sheet(content).iter_rows(min_row=2, values_only=True))


async def test_the_export_holds_exactly_the_rows_the_list_shows(
    submitted_application: str, published_contour, hodim_client, other_zone_hodim_client
) -> None:
    listed = await hodim_client.get("/api/v1/applications", params={"page_size": 100})
    listed_ids = {row["id"] for row in listed.json()["items"]}
    # A NON-EMPTY scope, or nothing below proves anything.
    assert submitted_application in listed_ids

    resp = await hodim_client.get(EXPORT, params={"lang": "ru"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.headers["x-export-truncated"] == "false"
    assert resp.headers["x-export-total"] == str(len(listed_ids))
    assert resp.headers["content-disposition"].startswith('attachment; filename="arizalar-')

    sheet = _sheet(resp.content)
    headers = [c.value for c in sheet[1]]
    assert headers[0] == "Номер" and headers[-1] == "ID"
    assert {row[-1] for row in _data_rows(resp.content)} == listed_ids

    # The other leshoz's hodim: the same contour filter empties both the list
    # and the file — the zone hides the row in both places, never in one.
    theirs = await other_zone_hodim_client.get(
        EXPORT, params={"contour_id": str(published_contour.id)}
    )
    assert theirs.status_code == 200
    assert _data_rows(theirs.content) == []
    assert theirs.headers["x-export-total"] == "0"


async def test_the_export_applies_the_same_filters_as_the_list(
    submitted_application: str, published_contour, hodim_client
) -> None:
    narrowed = await hodim_client.get(
        EXPORT, params={"contour_id": str(published_contour.id), "lang": "uz_latn"}
    )
    assert narrowed.status_code == 200
    assert [row[-1] for row in _data_rows(narrowed.content)] == [submitted_application]

    none = await hodim_client.get(EXPORT, params={"status": "REJECTED"})
    assert none.status_code == 200
    assert _data_rows(none.content) == []

    typo = await hodim_client.get(EXPORT, params={"status": "SUBMITED"})
    assert typo.status_code == 422  # the same literal the list validates


async def test_the_export_renders_labels_not_codes(
    submitted_application: str, published_contour, hodim_client
) -> None:
    resp = await hodim_client.get(
        EXPORT, params={"contour_id": str(published_contour.id), "lang": "uz_latn"}
    )
    (row,) = _data_rows(resp.content)
    headers = [c.value for c in _sheet(resp.content)[1]]
    by_header = dict(zip(headers, row, strict=True))

    assert by_header["Holati"] == "Yuborilgan"  # the label, not SUBMITTED
    assert by_header["Turi"] == "Yangi"
    assert by_header["Kontur"] == published_contour.number  # a number, not a UUID
    assert by_header["Ariza beruvchi"]  # resolved to a name
    assert by_header["Faoliyat turi"]  # resolved to a name
    assert isinstance(by_header["Raqam"], str) and by_header["Raqam"].startswith("RX-")
    assert by_header["ID"] == submitted_application

    in_russian = await hodim_client.get(
        EXPORT, params={"contour_id": str(published_contour.id), "lang": "ru"}
    )
    (row_ru,) = _data_rows(in_russian.content)
    headers_ru = [c.value for c in _sheet(in_russian.content)[1]]
    assert dict(zip(headers_ru, row_ru, strict=True))["Статус"] == "Подана"


async def test_the_export_truncates_at_the_cap_and_says_so(
    submitted_application: str, hodim_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_get_int = settings_store.get_int

    async def one(db, key: str) -> int:
        if key == "register_export_max_rows":
            return 1
        return await real_get_int(db, key)

    monkeypatch.setattr(settings_store, "get_int", one)

    listed = await hodim_client.get("/api/v1/applications", params={"page_size": 100})
    total = listed.json()["total"]
    assert total >= 1

    resp = await hodim_client.get(EXPORT)
    assert resp.status_code == 200
    assert resp.headers["x-export-total"] == str(total)
    assert resp.headers["x-export-rows"] == "1"
    assert resp.headers["x-export-truncated"] == ("true" if total > 1 else "false")
    assert len(_data_rows(resp.content)) == 1


async def test_the_file_and_the_list_agree_for_a_republic_wide_reader(
    submitted_application: str, prosecutor_client
) -> None:
    """The prosecutor's zone is the whole republic: the file holds the same
    ids the list does — neither wider (ruling #98) nor narrower.

    The list is read to its END, not as one page of 100: nothing rolls a test
    back (`tests/conftest.py`), so this worker's database holds every
    application the module's earlier tests filed, and once they pass a hundred
    a single page is a strict subset of the file (CI, 2026-09-13)."""
    listed_ids: set[str] = set()
    page = 1
    while True:
        listed = await prosecutor_client.get(
            "/api/v1/applications", params={"page": page, "page_size": 100}
        )
        assert listed.status_code == 200, listed.text
        body = listed.json()
        listed_ids |= {row["id"] for row in body["items"]}
        if page * 100 >= body["total"]:
            break
        page += 1
    resp = await prosecutor_client.get(EXPORT)
    assert resp.status_code == 200
    assert {row[-1] for row in _data_rows(resp.content)} == listed_ids


async def test_an_unknown_language_is_refused(hodim_client) -> None:
    assert (await hodim_client.get(EXPORT, params={"lang": "en"})).status_code == 422


async def test_the_export_route_is_not_shadowed_by_the_card_route(hodim_client) -> None:
    """`export.xlsx` is not a UUID: declared after `/{application_id}` it would
    be a 422 from the id parser rather than this route."""
    resp = await hodim_client.get(EXPORT)
    assert resp.status_code == 200, resp.text
    assert (await hodim_client.get(f"/api/v1/applications/{uuid.uuid4()}")).status_code == 404
