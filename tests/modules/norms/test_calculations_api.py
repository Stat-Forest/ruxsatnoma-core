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
from app.modules.auth.models import Applicant
from app.modules.gis.models import Contour
from app.modules.norms.models import Calculation
from tests.modules.auth.test_sessions import make_user

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
    The listing route filters by `application_id` alone with no ownership
    check (`norms/repo.py`), so this synthetic applicant need not match
    `applicant_client`'s own identity."""
    owner = await make_user(db, role_code="applicant", pinfl=f"1{uuid.uuid4().int % 10**13:013d}")
    applicant = Applicant(
        kind="individual", pinfl=owner.pinfl, name=owner.full_name, owner_user_id=owner.id
    )
    db.add(applicant)
    await db.flush()
    application = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=owner.id,
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


async def test_a_calculation_cannot_be_bound_to_an_application_yet(
    applicant_client: AsyncClient,
    published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """I4 (final review): `POST /calculations` persisted `application_id`
    verbatim — no FK (ruling 4 defers it to 3.9), no ownership check, and
    `calculations` is append-only, so a row bound to ANY application id could
    be inserted by any authenticated user and could never be deleted or
    corrected. 3.10 builds an invoice from "the newest row for the
    application", which makes a pre-seeded row a live under-billing vector the
    moment `applications` exists.

    Nothing in THIS stage can validate that id, and no legitimate caller has
    one yet, so it is refused at the schema edge until 3.9 opens it."""
    response = await applicant_client.post(
        "/api/v1/calculations",
        json={
            "application_id": str(uuid.uuid4()),
            "contour_id": str(published_contour.id),
            "activity_type_id": str(haymaking_activity_id),
            "period_from": "2026-06-01",
            "period_to": "2026-09-30",
            "quantity": "3",
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ERR-VAL-001"


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
