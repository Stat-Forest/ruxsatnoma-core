"""`GET /gis/contours/{id}/occupancy` — the three calendar states on a
capacity contour, the two states an EXCLUSIVE contour reads as (ruling #176),
the honest gap this track's report names (a non-grazing CAPACITY contour has
no committed-quantity data to split on yet), the refusals, and the "one
query, never one per day" property `repo.active_permit_periods` exists for.

No personal data — `test_no_personal_data.py`."""

from datetime import date
from decimal import Decimal

from app.main import create_app
from app.modules.occupancy import repo as occupancy_repo
from tests.conftest import make_client
from tests.modules.occupancy.conftest import issue_permit, make_published_norm, occupancy_url
from tests.modules.permits.conftest import count_queries


async def test_capacity_contour_shows_all_three_states(
    db, published_contour, leshoz, gis_user, approval_doc, grazing_activity_id, applicant_client
):
    """Decision #176's own worked example, in miniature: capacity 100,
    nothing committed reads `free`; 60 committed reads `partial` with the
    remainder stated (`40`); the whole capacity committed reads `full` — all
    from the permits' own date ranges, not a day-by-day walk."""
    await make_published_norm(
        db,
        contour=published_contour,
        activity_type_id=grazing_activity_id,
        gis_user=gis_user,
        approval_doc=approval_doc,
        max_sb=100,
    )
    await issue_permit(
        db,
        contour=published_contour,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        period_from=date(2028, 1, 11),
        period_to=date(2028, 1, 20),
        sb_load=Decimal("60"),
    )
    await issue_permit(
        db,
        contour=published_contour,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        period_from=date(2028, 1, 21),
        period_to=date(2028, 1, 31),
        sb_load=Decimal("100"),
    )
    await db.commit()

    resp = await applicant_client.get(
        occupancy_url(
            published_contour.id, grazing_activity_id, date(2028, 1, 1), date(2028, 1, 31)
        )
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["capacity"] == "100"
    assert body["unit"] == "sb"
    assert body["exclusive"] is False
    assert body["load_source"] == "permits"
    assert [
        (p["period_from"], p["period_to"], p["committed"], p["remaining"], p["result"])
        for p in body["periods"]
    ] == [
        ("2028-01-01", "2028-01-10", "0", "100", "free"),
        ("2028-01-11", "2028-01-20", "60.0000", "40.0000", "partial"),
        ("2028-01-21", "2028-01-31", "100.0000", "0.0000", "full"),
    ]


async def test_exclusive_contour_shows_free_and_full(
    db, published_contour, leshoz, haymaking_activity_id, applicant_client
):
    """Ruling #176's option a: no norm at all (no capacity) makes the contour
    EXCLUSIVE for this activity — `full` on every day an ACTIVE permit
    covers, `free` elsewhere, with no quantity involved at all."""
    await issue_permit(
        db,
        contour=published_contour,
        org=leshoz,
        activity_type_id=haymaking_activity_id,
        period_from=date(2028, 2, 10),
        period_to=date(2028, 2, 20),
        sb_load=None,
    )
    await db.commit()

    resp = await applicant_client.get(
        occupancy_url(
            published_contour.id, haymaking_activity_id, date(2028, 2, 1), date(2028, 2, 28)
        )
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["capacity"] is None
    assert body["exclusive"] is True
    assert body["load_source"] == "permits"
    assert [(p["period_from"], p["period_to"], p["result"]) for p in body["periods"]] == [
        ("2028-02-01", "2028-02-09", "free"),
        ("2028-02-10", "2028-02-20", "full"),
        ("2028-02-21", "2028-02-28", "free"),
    ]
    # No quantity number for a state that never had a quantity to state.
    assert all(p["committed"] is None and p["remaining"] is None for p in body["periods"])


async def test_exclusive_contour_with_no_permits_is_free_throughout(
    db, published_contour, haymaking_activity_id, applicant_client
):
    resp = await applicant_client.get(
        occupancy_url(
            published_contour.id, haymaking_activity_id, date(2028, 3, 1), date(2028, 3, 31)
        )
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exclusive"] is True
    assert len(body["periods"]) == 1
    assert body["periods"][0]["result"] == "free"


async def test_a_haymaking_permit_takes_its_hectares_off_the_calendar(
    db,
    published_contour,
    leshoz,
    gis_user,
    approval_doc,
    haymaking_activity_id,
    applicant_client,
):
    """A non-grazing activity commits its amount in `permits.quantity`, and the
    calendar must spend it.

    This test used to assert the opposite — that a haymaking permit could not
    move the calendar off `free`, because `permits` carried no committed-
    quantity column at all and `sb_load` is null for haymaking by
    construction. That was honest while it was true, and it stopped being
    true in the same wave: T6 added the column, and an occupied meadow drawing
    GREEN is precisely the failure this project keeps meeting — the refusal
    that quietly stops refusing. 20 ha of 50 taken leaves 30 and reads
    `partial`, on the days the permit actually covers and not one day more."""
    await make_published_norm(
        db,
        contour=published_contour,
        activity_type_id=haymaking_activity_id,
        gis_user=gis_user,
        approval_doc=approval_doc,
        capacity=Decimal("50"),
    )
    await issue_permit(
        db,
        contour=published_contour,
        org=leshoz,
        activity_type_id=haymaking_activity_id,
        period_from=date(2028, 4, 10),
        period_to=date(2028, 4, 20),
        sb_load=None,
        quantity=Decimal("20"),
    )
    await db.commit()

    resp = await applicant_client.get(
        occupancy_url(
            published_contour.id, haymaking_activity_id, date(2028, 4, 1), date(2028, 4, 30)
        )
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["capacity"] == "50.0000"
    assert body["unit"] == "ha"
    assert body["exclusive"] is False
    assert body["load_source"] == "permits"
    # Three stretches: free before the permit, partial while it runs, free
    # after it ends — the calendar's whole point is where the answer changes.
    assert [p["result"] for p in body["periods"]] == ["free", "partial", "free"]
    taken = body["periods"][1]
    assert taken["period_from"] == "2028-04-10"
    assert taken["period_to"] == "2028-04-20"
    assert taken["committed"] == "20.0000"
    assert taken["remaining"] == "30.0000"


async def test_period_reversed_is_refused(
    db, published_contour, grazing_activity_id, applicant_client
):
    resp = await applicant_client.get(
        occupancy_url(
            published_contour.id, grazing_activity_id, date(2028, 1, 31), date(2028, 1, 1)
        )
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "ERR-VAL-001"
    assert body["error"]["details"]["reason"] == "period_reversed"


async def test_unknown_contour_is_a_404(db, grazing_activity_id, applicant_client):
    resp = await applicant_client.get(
        occupancy_url(
            "00000000-0000-0000-0000-000000000000",  # type: ignore[arg-type]
            grazing_activity_id,
            date(2028, 1, 1),
            date(2028, 1, 31),
        )
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "ERR-SYS-003"


async def test_unknown_activity_type_is_refused(db, published_contour, applicant_client):
    resp = await applicant_client.get(
        occupancy_url(
            published_contour.id,
            "00000000-0000-0000-0000-000000000000",  # type: ignore[arg-type]
            date(2028, 1, 1),
            date(2028, 1, 31),
        )
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "ERR-VAL-001"
    assert body["error"]["details"]["reason"] == "unknown_activity_type"


async def test_an_anonymous_caller_gets_nothing(db, published_contour, grazing_activity_id):
    """Mirrors `gis.router`'s own anonymous gate on a contour read: no
    permission code, but a session IS required."""
    async with make_client(create_app(), lifespan=True) as anonymous:
        resp = await anonymous.get(
            occupancy_url(
                published_contour.id, grazing_activity_id, date(2028, 1, 1), date(2028, 1, 31)
            )
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "ERR-AUTH-002"


async def test_one_query_however_many_permits_or_however_long_the_window(
    db, published_contour, leshoz, grazing_activity_id
):
    """`repo.active_permit_periods` is what `occupancy.service` builds every
    sub-period boundary from — proving it costs ONE statement, whatever the
    permit count or the window length, is what stands behind "one query for
    the contour, never one per day"."""
    for month in range(1, 6):
        await issue_permit(
            db,
            contour=published_contour,
            org=leshoz,
            activity_type_id=grazing_activity_id,
            period_from=date(2028, month, 1),
            period_to=date(2028, month, 5),
            sb_load=Decimal("10"),
        )
    await db.commit()

    async with count_queries(db) as counter:
        periods = await occupancy_repo.active_permit_periods(
            db, published_contour.id, grazing_activity_id, date(2020, 1, 1), date(2035, 12, 31)
        )

    assert counter.value == 1, f"must cost one query, not {counter.value}"
    assert len(periods) == 5


async def test_a_permit_awaiting_signatures_already_shows_full(
    db, published_contour, leshoz, haymaking_activity_id, applicant_client
):
    """A permit is born `pending_signatures` and may sit there for weeks
    (ruling #99); issuance already refuses a competitor over it. The calendar
    must not paint those days green — green is the colour an applicant acts
    on, and a slot that is reserved but unsigned is not free."""
    await issue_permit(
        db,
        contour=published_contour,
        org=leshoz,
        activity_type_id=haymaking_activity_id,
        period_from=date(2028, 2, 10),
        period_to=date(2028, 2, 20),
        sb_load=None,
        status="pending_signatures",
    )
    await db.commit()

    resp = await applicant_client.get(
        occupancy_url(
            published_contour.id, haymaking_activity_id, date(2028, 2, 1), date(2028, 2, 28)
        )
    )

    assert resp.status_code == 200, resp.text
    assert [(p["period_from"], p["period_to"], p["result"]) for p in resp.json()["periods"]] == [
        ("2028-02-01", "2028-02-09", "free"),
        ("2028-02-10", "2028-02-20", "full"),
        ("2028-02-21", "2028-02-28", "free"),
    ]


def test_the_calendar_counts_exactly_the_statuses_the_gates_count() -> None:
    """`occupancy.repo` mirrors `permits.service.OCCUPYING_STATUSES` as a
    literal (a reader takes the table, never the service); this is the pin
    that keeps the calendar and the three gates answering the same question.
    A test may import what `app/` may not (module boundary)."""
    from app.modules.permits import service as permits_service

    assert occupancy_repo.OCCUPYING_STATUSES == permits_service.OCCUPYING_STATUSES
