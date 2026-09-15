"""Stage 13 (ruling #204): `GET /admin/ratings/export.xlsx` — the anonymous
comment feed on paper. Ruling #141 holds in the file exactly as on the screen:
no applicant, no permit — the id column is the RATING's own id, which names
nobody."""

import uuid

import pytest

from tests.conftest import assert_export_cut, export_cap, xlsx_rows
from tests.modules.permits.test_ratings import (
    other_org_ratings_client as other_org_ratings_client,  # noqa: F401
)
from tests.modules.permits.test_ratings import ratings_client as ratings_client  # noqa: F401
from tests.modules.permits.test_ratings import seeded_ratings as seeded_ratings  # noqa: F401

pytestmark = pytest.mark.asyncio

EXPORT = "/api/v1/admin/ratings/export.xlsx"
PERIOD = {"period_from": "2026-01-01", "period_to": "2026-12-31"}


async def test_the_file_mirrors_the_feed_and_names_nobody(
    ratings_client,
    seeded_ratings,
    grazing_activity_id: uuid.UUID,  # noqa: F811
) -> None:
    listed = (await ratings_client.get("/api/v1/admin/ratings", params=PERIOD)).json()
    assert listed["total"] == 3

    resp = await ratings_client.get(EXPORT, params={**PERIOD, "lang": "ru"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-export-total"] == "3"
    headers, rows = xlsx_rows(resp.content)
    assert len(rows) == 3
    assert headers == ["Дата", "Услуга", "Лесхоз", "Оценка", "Комментарий", "ID"]
    assert {row[3] for row in rows} == {3, 4, 5}
    assert {row[-1] for row in rows} == {str(r.id) for r in seeded_ratings}
    # The sheet's cells, not the zip's bytes, are what a reader sees:
    flat = " ".join(str(c) for row in rows for c in row)
    assert "RX-" not in flat and "@" not in flat

    # The feed's own filter narrows the file the same way.
    resp = await ratings_client.get(
        EXPORT, params={**PERIOD, "activity_type_id": str(grazing_activity_id)}
    )
    assert resp.status_code == 200
    headers, rows = xlsx_rows(resp.content)
    assert {row[3] for row in rows} == {3, 5}
    assert headers[0] == "Sana"

    with export_cap(1):
        resp = await ratings_client.get(EXPORT, params=PERIOD)
        assert resp.headers["x-export-total"] == "3"
        assert_export_cut(resp, cap=1)


async def test_another_leshoz_gets_an_empty_file_like_an_empty_feed(
    other_org_ratings_client,
    seeded_ratings,  # noqa: F811
) -> None:
    listed = (await other_org_ratings_client.get("/api/v1/admin/ratings", params=PERIOD)).json()
    resp = await other_org_ratings_client.get(EXPORT, params=PERIOD)
    assert resp.status_code == 200
    assert listed["items"] == [] and xlsx_rows(resp.content)[1] == []


async def test_without_the_permission_the_file_is_refused_like_the_feed(holder_client) -> None:
    listed = await holder_client.client.get("/api/v1/admin/ratings", params=PERIOD)
    resp = await holder_client.client.get(EXPORT, params=PERIOD)
    assert resp.status_code == listed.status_code == 403


async def test_a_missing_period_is_refused(ratings_client) -> None:  # noqa: F811
    # The period is required, exactly as on the feed.
    assert (await ratings_client.get(EXPORT)).status_code == 422
