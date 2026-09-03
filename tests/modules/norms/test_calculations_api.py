"""`POST /calculations` — the same arithmetic as `/calculations/preview`, but
persisted. Unlike a preview, a BLOCKING check here refuses with the mapped
ERR-NORM-00x (`checks.first_blocking_error`) instead of merely reporting it,
and every save is a NEW row: `calculations` is append-only (migration 0011),
so a recalculation for the same application is a second insert, never an
update (ruling 21)."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications.models import Application
from app.modules.gis.models import Contour
from app.modules.norms.models import Calculation

pytestmark = pytest.mark.asyncio


async def test_saving_a_calculation_writes_a_row_and_returns_it(
    applicant_client: AsyncClient,
    published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
    frozen_on_date: date,
) -> None:
    """`frozen_on_date` pins `on_date` inside the 412 000 `bhm` window — both
    the exact amount and the `bhm` value asserted below are only correct on
    one side of the seeded 2026-09-01 switch to 440 000 (lesson)."""
    response = await applicant_client.post(
        "/api/v1/calculations",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(haymaking_activity_id),
            "period_from": "2026-06-01",
            "period_to": "2026-09-30",
            "quantity": "4",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert uuid.UUID(body["id"])
    # 4 ha × 1.5 × 412 000 — same arithmetic as the preview endpoint, just
    # persisted; compared as Decimal since the NUMERIC(18,2) column round-trips
    # at its own scale, not the calculator's (lesson).
    assert Decimal(body["amount"]) == Decimal("2472000")
    # The row is self-explanatory years later without the database: the
    # parameter values it was computed from (bhm among them) live on it.
    assert body["input_snapshot"]["params"]["bhm"] == "412000"
    assert body["rule_code_version"] == "norms-1.0.0"


async def test_saving_over_the_limit_is_refused_with_the_mapped_error(
    applicant_client: AsyncClient,
    published_grazing_norm: uuid.UUID,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    published_coef_sb: None,
) -> None:
    """Unlike a preview, a save is a commitment: `checks.first_blocking_error`
    maps an exceeded limit onto ERR-NORM-002 and refuses outright, carrying
    the whole check list in `details` so the caller never has to re-run
    anything to see why."""
    response = await applicant_client.post(
        "/api/v1/calculations",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(grazing_activity_id),
            "period_from": "2026-05-01",
            "period_to": "2026-09-30",
            "items": [{"livestock_code": "cattle_adult", "count": 500}],
        },
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "ERR-NORM-002"
    limit = next(c for c in error["details"]["checks"] if c["check"] == "limit")
    assert limit["result"] == "fail"


async def test_a_second_save_adds_a_row_never_an_update(
    applicant_client: AsyncClient, published_contour: Contour, haymaking_activity_id: uuid.UUID
) -> None:
    """Ruling 21: `calculations` is append-only, so a recalculation is a
    second insert."""
    payload = {
        "contour_id": str(published_contour.id),
        "activity_type_id": str(haymaking_activity_id),
        "period_from": "2026-06-01",
        "period_to": "2026-09-30",
        "quantity": "4",
    }
    first = await applicant_client.post("/api/v1/calculations", json=payload)
    second = await applicant_client.post("/api/v1/calculations", json=payload | {"quantity": "5"})
    assert first.status_code == 201
    assert second.status_code == 201
    first_id, second_id = first.json()["id"], second.json()["id"]
    assert first_id != second_id

    for calculation_id in (first_id, second_id):
        fetched = await applicant_client.get(f"/api/v1/calculations/{calculation_id}")
        assert fetched.status_code == 200
        assert fetched.json()["id"] == calculation_id


async def test_the_history_for_an_application_lists_newest_first(
    applicant_client: AsyncClient,
    db: AsyncSession,
    published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """`GET /calculations?application_id=` is 3.10's entry point ("the newest
    row for the application"), so the filter and its ordering still need
    cover — but `POST` no longer accepts an `application_id` (I4), so the two
    rows are inserted the way STAGE 3.9 will write them, not through the
    route. The read side is unchanged by that fix and must keep working.

    `application_id` now needs a real `applications` row: stage 3.9a's
    migration 0015 closed `calculations.application_id`'s deferred FK (this
    test predates `applications` and originally used a bare `uuid.uuid4()`).

    **The application is `applicant_client`'s OWN as of stage 3.9a task 8
    (ruling 11).** It used to belong to a synthetic applicant, on the stated
    grounds that "the listing route filters by `application_id` alone with no
    ownership check" — which is exactly what ruling 11 closed: the route now
    answers an empty page for an application the caller has no claim on, so
    that shape asserted `total == 2` against `total == 0`. The ordering this
    test is about is unaffected; only whose application it runs on changed.
    """
    me = (await applicant_client.get("/api/v1/auth/me")).json()
    application = Application(
        applicant_id=uuid.UUID(me["applicant"]["id"]),
        submitted_by_user_id=uuid.UUID(me["user"]["id"]),
        on_behalf="self",
        channel="portal",
    )
    db.add(application)
    await db.flush()
    application_id = application.id
    written = []
    for amount in (Decimal("1000"), Decimal("2000")):
        row = Calculation(
            application_id=application_id,
            contour_id=published_contour.id,
            activity_type_id=haymaking_activity_id,
            rule_code_version="norms-1.0.0",
            input_snapshot={},
            amount=amount,
            breakdown=[],
        )
        db.add(row)
        await db.flush()
        written.append(str(row.id))

    listing = await applicant_client.get(
        "/api/v1/calculations", params={"application_id": str(application_id)}
    )
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == list(reversed(written))


async def test_get_calculation_by_id_returns_the_saved_row(
    applicant_client: AsyncClient, published_contour: Contour, haymaking_activity_id: uuid.UUID
) -> None:
    created = await applicant_client.post(
        "/api/v1/calculations",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(haymaking_activity_id),
            "period_from": "2026-06-01",
            "period_to": "2026-09-30",
            "quantity": "4",
        },
    )
    calculation_id = created.json()["id"]
    fetched = await applicant_client.get(f"/api/v1/calculations/{calculation_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == calculation_id


async def test_get_calculation_missing_id_is_404(applicant_client: AsyncClient) -> None:
    response = await applicant_client.get(f"/api/v1/calculations/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_calculation_can_only_be_bound_to_an_application_the_caller_may_touch(
    applicant_client: AsyncClient,
    db: AsyncSession,
    published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
    frozen_on_date: date,
) -> None:
    """**Rewritten by stage 3.9a task 5, not deleted** — this is the same
    concern the 3.7 version pinned, now asserting the real guard instead of the
    accident that stood in for it.

    3.7 (finding I4) typed `CalculationIn.application_id` as `None` so that
    `POST /calculations` — `get_current_user` and NO permission code — could
    not persist an unvalidated application id into an append-only table. 3.9a
    opened the field, because `applications.service.submit` needs it (ruling
    8), and landed two refusals in `service.save_calculation` in the same
    commit: the caller must OWN the application (or be staff entitled to review
    it), and the application must not be APPROVED or beyond.

    Both are asserted end to end in
    `tests/test_cross_module_journey.py::test_a_calculation_cannot_be_attached_
    to_an_application_through_the_write_path`, which has the paid/approved
    fixtures. What THIS module owns is the two ends of the range: an id that
    names nothing at all is 404 and never an `IntegrityError`/500 from the FK
    migration 0015 added, and an application the caller does own is saved and
    bound.
    """
    body = {
        "contour_id": str(published_contour.id),
        "activity_type_id": str(haymaking_activity_id),
        "period_from": "2026-06-01",
        "period_to": "2026-09-30",
        "quantity": "3",
    }

    unknown = await applicant_client.post(
        "/api/v1/calculations", json={**body, "application_id": str(uuid.uuid4())}
    )
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] == "ERR-SYS-003"

    # The caller's own DRAFT: allowed, and the binding is what is stored.
    me = (await applicant_client.get("/api/v1/auth/me")).json()
    application = Application(
        applicant_id=uuid.UUID(me["applicant"]["id"]),
        submitted_by_user_id=uuid.UUID(me["user"]["id"]),
        on_behalf="self",
        channel="portal",
        status="DRAFT",
    )
    db.add(application)
    await db.flush()

    mine = await applicant_client.post(
        "/api/v1/calculations", json={**body, "application_id": str(application.id)}
    )
    assert mine.status_code == 201, mine.text
    assert mine.json()["application_id"] == str(application.id)


# The guard's own vocabulary check moved to
# `test_calculation_application_guard.py` with the rename of
# `_APPLICATION_READ_CODES` -> `_APPLICATION_RECALCULATE_CODES` (review round
# 2, Critical 1): the name it asserted against documented the defect, and the
# whole predicate — entitlement, zone, and the actor-dependent status split —
# is covered in one place there rather than half here.


async def test_a_bare_calculation_is_readable_by_its_creator_and_by_nobody_else(
    applicant_client: AsyncClient,
    gis_specialist_client: AsyncClient,
    published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
    frozen_on_date: date,
) -> None:
    """Ruling 11's second half. A calculation with NO `application_id` is a
    bare price check: it names no application and belongs to no leshoz, so
    there is no ownership and no zone to ask about, and the only claim anybody
    has on it is having made it.

    `gis_specialist_client` is the proof that this is not merely "any staffer
    is refused by accident": it holds a real permission and a real zone — the
    zone that owns `published_contour`, at that — and is still told 404,
    because a staff code is a claim on an APPLICATION and this row has none.
    Both routes, since ruling 11 narrows both.
    """
    created = await applicant_client.post(
        "/api/v1/calculations",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(haymaking_activity_id),
            "period_from": "2026-06-01",
            "period_to": "2026-09-30",
            "quantity": "3",
        },
    )
    assert created.status_code == 201, created.text
    calculation_id = created.json()["id"]

    assert (await applicant_client.get(f"/api/v1/calculations/{calculation_id}")).status_code == 200
    refused = await gis_specialist_client.get(f"/api/v1/calculations/{calculation_id}")
    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "ERR-SYS-003"

    listed = await gis_specialist_client.get("/api/v1/calculations")
    assert listed.status_code == 200, listed.text
    assert calculation_id not in [item["id"] for item in listed.json()["items"]]


async def test_a_calculation_with_no_application_id_is_still_saved(
    applicant_client: AsyncClient,
    published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
    frozen_on_date,
) -> None:
    """The control for the refusal above: omitting the field is the normal,
    supported case in 3.7 and must keep working."""
    response = await applicant_client.post(
        "/api/v1/calculations",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(haymaking_activity_id),
            "period_from": "2026-06-01",
            "period_to": "2026-09-30",
            "quantity": "3",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["application_id"] is None
