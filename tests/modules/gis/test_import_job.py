"""The import is a job (ruling 6) and it is atomic (ruling 7)."""

import asyncio

import pytest
from sqlalchemy import func, select

from app.modules.gis import import_service
from app.modules.gis.models import Contour, ContourVersion, GisImport, LayerFeature
from tests.modules.gis.conftest import drain_pending_imports


@pytest.fixture(autouse=True)
async def _drain_leftover_imports(session_factory):
    """See `conftest.drain_pending_imports`: the claim is queue-wide, and the
    import fixtures commit, so a run interrupted halfway strands a `pending` row
    that every later run would otherwise claim ahead of its own. Autouse and
    listed first, so it runs before any `pending_import*` fixture creates this
    test's own row."""
    await drain_pending_imports(session_factory)


async def test_a_clean_batch_creates_contours_and_draft_versions(
    db, session_factory, pending_import
):
    processed = await import_service.process_pending(session_factory)
    assert processed == 1
    await db.refresh(pending_import)
    assert pending_import.status == "review"
    assert pending_import.stats["created"] == 2
    versions = await db.execute(
        select(ContourVersion).where(ContourVersion.import_id == pending_import.id)
    )
    assert len(list(versions.scalars().all())) == 2


async def test_one_bad_row_rolls_the_whole_batch_back(
    db, session_factory, pending_import_with_a_broken_row
):
    await import_service.process_pending(session_factory)
    await db.refresh(pending_import_with_a_broken_row)
    assert pending_import_with_a_broken_row.status == "failed"
    assert pending_import_with_a_broken_row.error_report[0]["code"] == "empty_geometry"
    left = await db.execute(
        select(ContourVersion).where(
            ContourVersion.import_id == pending_import_with_a_broken_row.id
        )
    )
    assert list(left.scalars().all()) == []


async def test_a_missing_mandatory_attribute_is_reported_for_the_row_that_lacks_it(
    db, session_factory, pending_import_missing_number
):
    """Caught in the pure-Python mapping pass, before a single write — so a
    wrong attribute map produces the WHOLE file's report in one run rather than
    one bad row per attempt (tz/07: "row number plus error type")."""
    await import_service.process_pending(session_factory)
    await db.refresh(pending_import_missing_number)
    assert pending_import_missing_number.status == "failed"
    assert pending_import_missing_number.error_report[0]["code"] == "missing_attribute"
    assert pending_import_missing_number.error_report[0]["row"] == 1


async def test_a_failure_halfway_through_the_writes_undoes_the_rows_already_written(
    db, session_factory, pending_import_non_polygon_on_row_1
):
    """The savepoint's own case (ruling 7): the parse succeeded, row 0's contour
    and version were already INSERTED, and row 1 turns out not to be polygonal.
    Everything the batch wrote must be gone — while the failure record itself
    survives, which is exactly why the writes live in a nested transaction and
    the report does not."""
    await import_service.process_pending(session_factory)
    await db.refresh(pending_import_non_polygon_on_row_1)
    assert pending_import_non_polygon_on_row_1.status == "failed"
    assert pending_import_non_polygon_on_row_1.error_report[0]["code"] == "invalid_geometry"
    assert pending_import_non_polygon_on_row_1.error_report[0]["row"] == 1
    left = await db.execute(
        select(func.count())
        .select_from(Contour)
        .where(Contour.organization_id == pending_import_non_polygon_on_row_1.organization_id)
    )
    assert left.scalar_one() == 0  # row 0's contour is gone too
    versions = await db.execute(
        select(func.count())
        .select_from(ContourVersion)
        .where(ContourVersion.import_id == pending_import_non_polygon_on_row_1.id)
    )
    assert versions.scalar_one() == 0


async def test_a_non_contour_layer_keeps_its_unmapped_attributes_in_props(
    db, session_factory, pending_import_restrictions
):
    """Ruling 13: the narrow number/declared-area mapping is a CONTOUR-layer
    rule (the source file's tenant names and debts are personal data and must
    never enter the spatial layer). Every other layer stores what it was not
    asked about in `props jsonb`, which is what that column is for."""
    await import_service.process_pending(session_factory)
    await db.refresh(pending_import_restrictions)
    assert pending_import_restrictions.status == "review"
    feature = (
        (
            await db.execute(
                select(LayerFeature).where(LayerFeature.import_id == pending_import_restrictions.id)
            )
        )
        .scalars()
        .one()
    )
    assert feature.status == "draft"
    assert feature.name == {"ru": "Water protection zone"}
    assert feature.props == {"note": "SanPiN", "rank": 2}  # "title" was mapped, so not repeated
    assert feature.approval_doc_id == pending_import_restrictions.approval_doc_id


async def test_an_area_mismatch_is_a_warning_and_does_not_stop_the_import(
    db, session_factory, pending_import_area_mismatch
):
    """Ruling 2 — 36 of Burchmulla's 151 features would otherwise be blocked."""
    await import_service.process_pending(session_factory)
    await db.refresh(pending_import_area_mismatch)
    assert pending_import_area_mismatch.status == "review"
    warning = pending_import_area_mismatch.stats["warnings"][0]
    assert warning["code"] == "area_mismatch"
    assert warning["declared_ha"] == 2.6
    assert warning["computed_ha"] > 85


async def test_a_duplicate_contour_number_gets_a_suffix(
    db, session_factory, pending_import_duplicate_numbers
):
    """Ruling 11 — 92 distinct numbers across 151 features in the real file. Both
    rows carry the same number in the source; the second gets "/2"."""
    await import_service.process_pending(session_factory)
    numbers = await db.execute(
        select(Contour.number).where(
            Contour.organization_id == pending_import_duplicate_numbers.organization_id
        )
    )
    assert sorted(numbers.scalars().all()) == ["14515q", "14515q/2"]
    await db.refresh(pending_import_duplicate_numbers)
    assert pending_import_duplicate_numbers.stats["warnings"][0]["code"] == "duplicate_number"


async def test_a_projected_source_lands_in_uzbekistan(db, session_factory, pending_import_utm42):
    """Decision #13 end to end: a UTM zone 42N file is stored as WGS84 degrees.
    The parser reports the SRID, PostGIS does the transform (Task 7)."""
    await import_service.process_pending(session_factory)
    version = (
        (
            await db.execute(
                select(ContourVersion).where(ContourVersion.import_id == pending_import_utm42.id)
            )
        )
        .scalars()
        .first()
    )
    assert version is not None
    lon, lat = (
        await db.execute(
            select(
                func.ST_X(func.ST_Centroid(ContourVersion.geom)),
                func.ST_Y(func.ST_Centroid(ContourVersion.geom)),
            ).where(ContourVersion.id == version.id)
        )
    ).one()
    assert 55 < lon < 74 and 37 < lat < 46  # the bounding box of Uzbekistan


async def test_the_initiator_is_notified_when_the_import_finishes(
    db, session_factory, pending_import
):
    from app.modules.notifications.models import Notification

    await import_service.process_pending(session_factory)
    rows = await db.execute(
        select(Notification).where(
            Notification.event_code == "gis.import.finished",
            Notification.recipient_user_id == pending_import.started_by,
        )
    )
    assert list(rows.scalars().all())


async def test_two_workers_do_not_process_the_same_import(session_factory, pending_import):
    """FOR UPDATE SKIP LOCKED, the same claim idiom as the outbox worker."""
    results = await asyncio.gather(
        import_service.process_pending(session_factory),
        import_service.process_pending(session_factory),
    )
    assert sorted(results) == [0, 1]


async def test_an_empty_queue_is_not_an_error(session_factory):
    """The autouse drain above already emptied the queue; this asserts the job
    answers 0 rather than raising when there is nothing to claim."""
    assert await import_service.process_pending(session_factory) == 0


async def test_the_batch_carries_the_import_and_its_basis_document_onto_every_version(
    db, session_factory, pending_import
):
    """Ruling 3: one basis document for the whole batch, stamped onto each
    version at creation — the `published_needs_doc` CHECK then holds for all 151
    of them without anyone producing 151 decrees."""
    await import_service.process_pending(session_factory)
    rows = (
        (
            await db.execute(
                select(ContourVersion).where(ContourVersion.import_id == pending_import.id)
            )
        )
        .scalars()
        .all()
    )
    assert {v.status for v in rows} == {"draft"}
    assert {v.source for v in rows} == {"import"}
    assert {v.approval_doc_id for v in rows} == {pending_import.approval_doc_id}
    assert {v.version_no for v in rows} == {1}


async def test_a_failed_import_still_notifies_the_initiator(
    db, session_factory, pending_import_with_a_broken_row
):
    """A 20-minute import must not need the browser to stay open (ruling 6) —
    and that is just as true when it fails as when it succeeds."""
    from app.modules.notifications.models import Notification

    await import_service.process_pending(session_factory)
    rows = await db.execute(
        select(Notification).where(
            Notification.event_code == "gis.import.finished",
            Notification.recipient_user_id == pending_import_with_a_broken_row.started_by,
        )
    )
    assert list(rows.scalars().all())


async def test_a_missing_stored_object_fails_the_import_instead_of_crashing_the_job(
    db, session_factory, pending_import
):
    """The MinIO object is gone (a bucket wiped, a lifecycle rule) — the import
    is unrunnable, but the job must keep draining the queue rather than raise
    once per tick forever."""
    import uuid

    from app.core.models import MediaFile

    file = await db.get(MediaFile, pending_import.file_id)
    assert file is not None
    # Randomised, not a fixed literal: `media_files.storage_key` is UNIQUE and
    # this row is committed, so a constant would collide with the previous run
    # of this very test in the shared, persistent test DB (lesson).
    file.storage_key = f"test/gone-{uuid.uuid4().hex}"
    await db.commit()

    assert await import_service.process_pending(session_factory) == 1
    await db.refresh(pending_import)
    assert pending_import.status == "failed"
    assert pending_import.error_report[0]["code"] == "file_missing"


async def test_a_pending_row_that_is_not_ready_is_left_for_the_next_tick(
    db, session_factory, pending_import
):
    """Only `pending` rows are claimed. A row already being processed by another
    worker (or left `processing` by a crashed one) is not re-claimed here — the
    status column is the whole claim condition, next to the row lock."""
    pending_import.status = "processing"
    await db.commit()
    assert await import_service.process_pending(session_factory) == 0
    left = await db.get(GisImport, pending_import.id)
    assert left is not None
