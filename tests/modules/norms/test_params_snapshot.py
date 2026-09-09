"""Integration coverage for `norms.params` — the only place that turns
database rows into a `ParamSnapshot`. The calculator itself never queries
anything (see `test_calculator.py`); this file proves the loader wires the
real tables (`rule_parameters`, `tariffs`, `norms`) correctly."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import MediaFile
from app.db import make_session_factory
from app.modules.auth.models import User
from app.modules.gis.models import Contour
from app.modules.norms import params
from app.modules.norms import service as norms_service
from app.modules.norms.calculator import CalcRequest
from app.modules.norms.models import Norm

pytestmark = pytest.mark.asyncio


def _request(*, on_date: date, activity_code: str = "haymaking") -> CalcRequest:
    """A minimal, otherwise-unused request — only `on_date`/`period_from`/
    `period_to` matter to the loader; the arithmetic fields are irrelevant
    here since nothing calls `calculate` in this file."""
    return CalcRequest(
        activity_code=activity_code,
        on_date=on_date,
        period_from=on_date,
        period_to=on_date,
        area_ha=Decimal("1"),
        items=(),
        quantity=Decimal("1"),
        benefit_code=None,
    )


async def test_load_limit_params_returns_the_bhm_in_force_on_the_given_date(
    db: AsyncSession,
) -> None:
    """Ruling 7: the seeded `bhm` switches from 412 000 to 440 000 on
    2026-09-01 — `load_limit_params` must return whichever value is in force
    ON THE DATE ASKED, never today's."""
    before = await params.load_limit_params(db, on_date=date(2026, 8, 31))
    after = await params.load_limit_params(db, on_date=date(2026, 9, 1))
    assert before["bhm"] == "412000"
    assert after["bhm"] == "440000"
    assert before["safety_reserve"] == "0.85"
    assert before["sb_feed_norm"] == "3.74"


async def test_a_draft_coefficient_is_absent_from_the_snapshot(db: AsyncSession) -> None:
    """The ten `coef_sb:*` rows ship as drafts (ruling 8); `load_snapshot`
    only ever reads published rows (`repo.effective_parameters_by_prefix`),
    so `coef_sb:cattle_adult` is simply missing from `values` until a central
    admin publishes it — turning that absence into `ERR-NORM-004` is the
    calculator's job (`test_a_missing_parameter_names_itself`), not this
    loader's."""
    snapshot = await params.load_snapshot(
        db,
        request=_request(on_date=date(2026, 8, 30), activity_code="grazing"),
        contour_id=None,
        activity_type_id=uuid.uuid4(),
    )
    assert "coef_sb:cattle_adult" not in snapshot.values
    assert "coef_sb:sheep_goat_6m" not in snapshot.values


async def test_a_published_override_makes_the_coefficient_and_its_group_visible(
    db: AsyncSession, published_coef_sb: None
) -> None:
    snapshot = await params.load_snapshot(
        db,
        request=_request(on_date=date(2026, 8, 30), activity_code="grazing"),
        contour_id=None,
        activity_type_id=uuid.uuid4(),
    )
    assert snapshot.values["coef_sb:cattle_adult"] == "6.0"
    assert snapshot.values["tariff_group:cattle_adult"] == "large_adult"


async def test_load_snapshot_returns_the_tariffs_in_force_for_the_activity(
    db: AsyncSession, grazing_activity_id: uuid.UUID, haymaking_activity_id: uuid.UUID
) -> None:
    """VMQ 278 seeds four grazing rows, one per livestock group, and one
    haymaking row with `livestock_group IS NULL`."""
    grazing = await params.load_snapshot(
        db,
        request=_request(on_date=date(2026, 8, 30), activity_code="grazing"),
        contour_id=None,
        activity_type_id=grazing_activity_id,
    )
    assert len(grazing.tariffs) == 4
    assert {t.livestock_group for t in grazing.tariffs} == {
        "large_adult",
        "large_young",
        "small_adult",
        "small_young",
    }

    haymaking = await params.load_snapshot(
        db,
        request=_request(on_date=date(2026, 8, 30), activity_code="haymaking"),
        contour_id=None,
        activity_type_id=haymaking_activity_id,
    )
    assert len(haymaking.tariffs) == 1
    assert haymaking.tariffs[0].livestock_group is None
    assert haymaking.tariffs[0].coefficient == Decimal("1.50")


async def test_a_contour_with_no_permits_on_it_reports_a_measured_zero(
    db: AsyncSession, published_contour: Contour, haymaking_activity_id: uuid.UUID
) -> None:
    """Ruling 12, second half. This asserted `load_source == "none"` while
    `LOAD_PROVIDERS` was empty — the placeholder that kept a caller from reading
    the zero as a measurement. 3.11a registers `permits.service.load_provider`
    (`app/event_subscriptions.py`), so the zero now means "no permit commits a
    single head on this contour" and the source says who measured it.

    Kept rather than deleted: the snapshot is what `calculations.input_snapshot`
    freezes forever, so which of the two a stored calculation recorded is a
    legally meaningful difference, and this is where it is pinned."""
    snapshot = await params.load_snapshot(
        db,
        request=_request(on_date=date(2026, 8, 30), activity_code="haymaking"),
        contour_id=published_contour.id,
        activity_type_id=haymaking_activity_id,
    )
    assert snapshot.load_sb == Decimal("0")
    assert snapshot.load_source == "permits"


async def test_a_calculation_with_no_contour_gets_no_norm_and_no_load(
    db: AsyncSession, haymaking_activity_id: uuid.UUID
) -> None:
    """A calculation not yet tied to a contour (`contour_id=None`) cannot have
    a per-contour norm or a per-contour committed load — `load_snapshot` must
    not try `effective_norm`/`committed_load_sb` against a `None` id, it must
    simply report the same empty shape a contour with nothing on it would."""
    snapshot = await params.load_snapshot(
        db,
        request=_request(on_date=date(2026, 8, 30), activity_code="haymaking"),
        contour_id=None,
        activity_type_id=haymaking_activity_id,
    )
    assert snapshot.norm is None
    assert snapshot.load_sb == Decimal("0")
    assert snapshot.load_source == "none"


async def test_load_snapshot_returns_the_published_norm_in_force(
    db: AsyncSession,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    gis_user: User,
    approval_doc: MediaFile,
) -> None:
    """The norm's own `max_sb` is already frozen at publication (Task 4) —
    `load_snapshot` reads it back as-is, it does not recompute it."""
    norm = Norm(
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        yield_c_per_ha=Decimal("12.0"),
        season={"windows": [{"from": "04-01", "to": "10-31"}]},
        rotation={"rest_years": []},
        max_sb=27,
        effective_from=date(2020, 1, 1),
        status="published",
        approval_doc_id=approval_doc.id,
        created_by=gis_user.id,
        approved_by=gis_user.id,
    )
    db.add(norm)
    await db.flush()

    snapshot = await params.load_snapshot(
        db,
        request=_request(on_date=date(2026, 8, 30), activity_code="grazing"),
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
    )
    assert snapshot.norm is not None
    assert snapshot.norm.max_sb == 27
    assert snapshot.norm.yield_c_per_ha == Decimal("12.0")
    assert snapshot.norm.season == {"windows": [{"from": "04-01", "to": "10-31"}]}


async def test_published_coef_sb_does_not_leak_a_sibling_fixtures_flushed_row(
    published_contour: Contour,
    published_coef_sb: None,
    engine,
) -> None:
    """Fix-round 1, finding 5, regression pin. `published_coef_sb` used to
    take the test's own `db` fixture and call `db.commit()` directly — the
    `db` fixture's isolation is `yield session; await session.rollback()`,
    and `published_contour` (via `make_contour`/`make_version`) only
    `add()`+`flush()`es on that SAME session, relying on that rollback for
    cleanup. Committing the shared session would have committed the contour
    too, permanently, into the shared test database.

    Listing `published_contour` BEFORE `published_coef_sb` (pytest
    instantiates a test's fixtures left-to-right) reproduces the exact
    ordering the old bug needed: the contour is flushed first, then
    `published_coef_sb` used to commit everything pending. Checking through a
    THIRD, wholly independent session — never the test's own `db` — proves
    the contour was never actually persisted; only `published_coef_sb`'s own
    rows were, through its own session, and those are cleaned up in its own
    teardown."""
    factory = make_session_factory(engine)
    async with factory() as fresh:
        row = await fresh.execute(
            text("SELECT 1 FROM contours WHERE id = :id"), {"id": published_contour.id}
        )
        assert row.first() is None, (
            "a flush-only sibling fixture's row became visible on an unrelated "
            "session — published_coef_sb's commit leaked it"
        )


# --- Ruling #176 (stage 9): the two NEW seams, and which one a request needs.


async def test_a_capacity_norm_loads_the_capacity_load_seam_not_the_exclusivity_one(
    db: AsyncSession,
    published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
    gis_user: User,
    approval_doc: MediaFile,
) -> None:
    """A haymaking norm WITH a `capacity` resolves through
    `CAPACITY_LOAD_PROVIDERS` — `occupied_until_source` must stay `"none"`
    (never asked, because there was no need to)."""
    norm = Norm(
        contour_id=published_contour.id,
        activity_type_id=haymaking_activity_id,
        capacity=Decimal("10"),
        effective_from=date(2020, 1, 1),
        status="published",
        approval_doc_id=approval_doc.id,
        created_by=gis_user.id,
        approved_by=gis_user.id,
    )
    db.add(norm)
    await db.flush()

    async def provider(db_, contour_id, activity_type_id, period_from, period_to):
        return Decimal("4")

    norms_service.CAPACITY_LOAD_PROVIDERS.append(provider)
    try:
        snapshot = await params.load_snapshot(
            db,
            request=_request(on_date=date(2026, 8, 30), activity_code="haymaking"),
            contour_id=published_contour.id,
            activity_type_id=haymaking_activity_id,
        )
        assert snapshot.norm is not None
        assert snapshot.norm.capacity == Decimal("10")
        assert snapshot.capacity_load == Decimal("4")
        assert snapshot.capacity_load_source == "permits"
        assert snapshot.occupied_until is None
        assert snapshot.occupied_until_source == "none"
    finally:
        norms_service.CAPACITY_LOAD_PROVIDERS.remove(provider)


async def test_a_capacity_less_norm_loads_the_exclusivity_seam_not_the_load_one(
    db: AsyncSession, published_contour: Contour, haymaking_activity_id: uuid.UUID
) -> None:
    """No norm at all (so no capacity) resolves through
    `EXCLUSIVITY_PROVIDERS` instead — `capacity_load_source` must stay
    `"none"` (never asked: a sum could never answer "which day is it free")."""

    async def provider(db_, contour_id, activity_type_id, period_from, period_to):
        return date(2026, 12, 31)

    norms_service.EXCLUSIVITY_PROVIDERS.append(provider)
    try:
        snapshot = await params.load_snapshot(
            db,
            request=_request(on_date=date(2026, 8, 30), activity_code="haymaking"),
            contour_id=published_contour.id,
            activity_type_id=haymaking_activity_id,
        )
        assert snapshot.norm is None
        assert snapshot.occupied_until == date(2026, 12, 31)
        assert snapshot.occupied_until_source == "permits"
        assert snapshot.capacity_load == Decimal("0")
        assert snapshot.capacity_load_source == "none"
    finally:
        norms_service.EXCLUSIVITY_PROVIDERS.remove(provider)


async def test_grazing_never_touches_either_new_seam(
    db: AsyncSession,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    gis_user: User,
    approval_doc: MediaFile,
) -> None:
    """Grazing resolves its capacity through `max_sb`/`LOAD_PROVIDERS`
    alone — registering a capacity-load provider must have no effect on a
    grazing snapshot even when a grazing norm with a frozen `max_sb` is in
    force, proving `load_snapshot`'s branch is keyed on the ACTIVITY, not
    merely on "a capacity resolved"."""
    norm = Norm(
        contour_id=published_contour.id,
        activity_type_id=grazing_activity_id,
        yield_c_per_ha=Decimal("12.0"),
        max_sb=27,
        effective_from=date(2020, 1, 1),
        status="published",
        approval_doc_id=approval_doc.id,
        created_by=gis_user.id,
        approved_by=gis_user.id,
    )
    db.add(norm)
    await db.flush()

    async def unexpected(db_, contour_id, activity_type_id, period_from, period_to):
        raise AssertionError("grazing must never call the capacity-load seam")

    norms_service.CAPACITY_LOAD_PROVIDERS.append(unexpected)
    try:
        snapshot = await params.load_snapshot(
            db,
            request=_request(on_date=date(2026, 8, 30), activity_code="grazing"),
            contour_id=published_contour.id,
            activity_type_id=grazing_activity_id,
        )
        assert snapshot.norm is not None
        assert snapshot.norm.max_sb == 27
        assert snapshot.capacity_load == Decimal("0")
        assert snapshot.capacity_load_source == "none"
        assert snapshot.occupied_until_source == "none"
    finally:
        norms_service.CAPACITY_LOAD_PROVIDERS.remove(unexpected)
