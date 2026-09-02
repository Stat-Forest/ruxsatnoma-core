"""Task 6: the two seams `gis` (3.6a) and `norms` (3.7) shipped empty.

Both have been answering with an explicit placeholder — `occupancy_source:
"none"`, `load_source: "none"` — since the stages that opened them, precisely so
that nobody could read a zero as a measurement. Registering this module's two
providers is what flips both to `"permits"` system-wide.

The fixtures below are module-local on purpose. `contour` has to be independent
of any permit (the suspended case asserts an EMPTY contour), and the permits
themselves have to sit on that ONE contour in three different statuses — which
the package-level `active_permit` (four real ERI signatures on a contour of its
own) cannot express. `active_permit_on_contour` is named apart from it for the
same reason: two fixtures with one name, one shadowing the other, is how a later
reader ends up asserting against the wrong permit.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import MediaFile
from app.modules.admin.models import Organization
from app.modules.gis.models import Contour, GisLayer
from app.modules.permits.models import Permit
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
from tests.modules.permits.conftest import count_queries, make_permit_on_contour


@pytest.fixture
async def contour(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization, approval_doc: MediaFile
) -> Contour:
    """One published contour, with no permit on it until a fixture below adds
    one. A random box, never a fixed one: a committed literal accumulates
    neighbours across runs on this shared database (lesson)."""
    row = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db, row.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    return row


@pytest.fixture
async def version_id(db: AsyncSession, contour: Contour) -> uuid.UUID:
    from app.modules.gis import service as gis_service

    version = await gis_service.published_version(db, contour.id)
    assert version is not None
    return version.id


@pytest.fixture
async def active_permit_on_contour(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> Permit:
    return await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
    )


@pytest.fixture
async def expired_permit(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> Permit:
    """A permit that ran its course on the SAME contour — the area it held is
    free again, and the conditional heads it committed are gone with it."""
    return await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="expired",
        area_ha=Decimal("7.0000"),
        sb_load=Decimal("11.0000"),
    )


@pytest.fixture
async def suspended_permit(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    grazing_activity_id: uuid.UUID,
) -> Permit:
    return await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="suspended",
    )


@pytest.fixture
async def many_contours(
    db: AsyncSession, contours_layer: GisLayer, leshoz: Organization
) -> list[Contour]:
    """A page's worth of contours — the shape `gis.service.list_contours` hands
    the provider. No versions needed: occupancy is read off `permits`, and this
    fixture exists to count round-trips, not to publish geometry."""
    rows = [await make_contour(db, contours_layer, leshoz) for _ in range(20)]
    await db.flush()
    return rows


async def test_occupancy_counts_only_active_permits(
    db: AsyncSession,
    contour: Contour,
    active_permit_on_contour: Permit,
    expired_permit: Permit,
) -> None:
    """Ruling 11. An expired permit frees the area it held."""
    from app.modules.permits import service

    result = await service.occupancy_provider(db, [contour.id])
    assert result[contour.id] == active_permit_on_contour.area_ha


async def test_a_suspended_permit_occupies_nothing(
    db: AsyncSession, contour: Contour, suspended_permit: Permit
) -> None:
    """Ruling 11: a suspended permit is not in use. Written now, because this
    stage owns the query and 3.11b only adds the status that reaches it."""
    from app.modules.permits import service

    assert (await service.occupancy_provider(db, [contour.id])).get(
        contour.id, Decimal("0")
    ) == Decimal("0")


async def test_occupancy_answers_a_whole_page_in_one_query(
    db: AsyncSession, many_contours: list[Contour]
) -> None:
    """3.6a reshaped this seam to batch specifically so this registration would
    not become N round-trips. A loop here silently undoes that."""
    from app.modules.permits import service

    ids = [c.id for c in many_contours]
    async with count_queries(db) as counter:
        await service.occupancy_provider(db, ids)
    assert counter.value == 1, f"{len(ids)} contours must cost one query, not {counter.value}"


async def test_an_empty_page_costs_no_query_at_all(db: AsyncSession) -> None:
    """`list_contours` calls the seam for every page, including one that matched
    nothing. `IN ()` is a statement with no possible answer — skip it."""
    from app.modules.permits import service

    async with count_queries(db) as counter:
        assert await service.occupancy_provider(db, []) == {}
    assert counter.value == 0


async def test_load_sums_overlapping_active_permits_only(
    db: AsyncSession, contour: Contour, active_permit_on_contour: Permit
) -> None:
    """The permit runs 2027-05-01..2027-09-30 with sb_load 40."""
    from app.modules.permits import service

    overlapping = await service.load_provider(db, contour.id, date(2027, 6, 1), date(2027, 7, 1))
    assert overlapping == Decimal("40.0000")

    apart = await service.load_provider(db, contour.id, date(2028, 6, 1), date(2028, 7, 1))
    assert apart == Decimal("0")


async def test_load_counts_a_period_that_only_touches_the_permits_last_day(
    db: AsyncSession, contour: Contour, active_permit_on_contour: Permit
) -> None:
    """`period_to` is inclusive on both sides of the comparison: a request
    starting on the permit's last day still shares that day with it, and heads
    grazing the same hectares on the same day are committed twice over.

    The RIGHT edge, exercising `Permit.period_to >= :period_from` alone — the
    permit begins before the request either way, so the other half of the
    predicate is true in both asserts and cannot be what decides them."""
    from app.modules.permits import service

    assert await service.load_provider(
        db, contour.id, date(2027, 9, 30), date(2027, 12, 31)
    ) == Decimal("40.0000")
    assert await service.load_provider(
        db, contour.id, date(2027, 10, 1), date(2027, 12, 31)
    ) == Decimal("0")


async def test_load_ignores_a_period_that_ends_before_the_permit_begins(
    db: AsyncSession, contour: Contour, active_permit_on_contour: Permit
) -> None:
    """The LEFT edge, and the half of the predicate nothing else reaches: both
    asserts here run entirely before the permit's own `period_to`, so
    `Permit.period_to >= :period_from` is true in each and only
    `Permit.period_from <= :period_to` can decide them. Deleted, that clause
    left every other test in this file green (review, Important 1) — a spring
    request would then have carried the whole summer's committed load, and the
    limit check would have refused a herd the contour had room for.

    The permit runs 2027-05-01..2027-09-30: a request ending the day before it
    opens shares nothing with it, and one ending on its first day shares that
    day."""
    from app.modules.permits import service

    assert await service.load_provider(
        db, contour.id, date(2027, 1, 1), date(2027, 4, 30)
    ) == Decimal("0")
    assert await service.load_provider(
        db, contour.id, date(2027, 1, 1), date(2027, 5, 1)
    ) == Decimal("40.0000")


async def test_a_suspended_permit_commits_no_load(
    db: AsyncSession, contour: Contour, suspended_permit: Permit
) -> None:
    """The load half of ruling 11, for the same reason: a suspended permit is
    not in use, so its herd is not on the contour."""
    from app.modules.permits import service

    assert await service.load_provider(
        db, contour.id, date(2027, 6, 1), date(2027, 7, 1)
    ) == Decimal("0")


async def test_a_permit_with_no_sb_load_contributes_nothing(
    db: AsyncSession,
    contour: Contour,
    version_id: uuid.UUID,
    leshoz: Organization,
    apiary_activity_id: uuid.UUID,
) -> None:
    """`sb_load` is null for an activity that commits no conditional-head load at
    all (haymaking, apiaries) — `SUM` skips nulls, and the answer is a real
    `Decimal("0")` rather than the `None` an unguarded SUM returns."""
    from app.modules.permits import service

    await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=apiary_activity_id,
        status="active",
        sb_load=None,
    )
    assert await service.load_provider(
        db, contour.id, date(2027, 6, 1), date(2027, 7, 1)
    ) == Decimal("0")


async def test_gis_reports_the_source_as_permits_once_registered(
    db: AsyncSession, contour: Contour, active_permit_on_contour: Permit
) -> None:
    """The user-visible half: `occupancy_source` flips from "none" to "permits"
    and `s_available_ha` stops equalling the full area."""
    from app.modules.gis import service as gis_service

    totals, source = await gis_service.occupancy_map(db, [contour.id])
    assert source == "permits"
    assert totals[contour.id] > Decimal("0")


async def test_norms_reports_the_load_source_as_permits(
    db: AsyncSession, contour: Contour, active_permit_on_contour: Permit
) -> None:
    from app.modules.norms import service as norms_service

    total, source = await norms_service.committed_load_sb(
        db, contour.id, date(2027, 6, 1), date(2027, 7, 1)
    )
    assert source == "permits"
    assert total == Decimal("40.0000")


def test_both_providers_are_registered_exactly_once() -> None:
    """Ruling P4. `tests/conftest.py::_isolate_subscriptions` calls
    `register_event_subscriptions()` for EVERY test and snapshots only
    `events._SUBSCRIBERS` — the two provider lists are separate globals it never
    restores. A bare `.append()` therefore adds one copy per test and occupancy
    silently doubles, then triples; the failure reads as pollution rather than as
    a registration bug, and passes when this file is run alone.
    """
    from app.event_subscriptions import register_event_subscriptions
    from app.modules.gis import service as gis_service
    from app.modules.norms import service as norms_service
    from app.modules.permits import service as permits_service

    for _ in range(3):
        register_event_subscriptions()

    assert gis_service.OCCUPANCY_PROVIDERS.count(permits_service.occupancy_provider) == 1
    assert norms_service.LOAD_PROVIDERS.count(permits_service.load_provider) == 1
