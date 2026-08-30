"""The import is a job (ruling 6) and it is atomic (ruling 7)."""

import asyncio

from sqlalchemy import func, select

from app.modules.gis import import_service, importer
from app.modules.gis.models import Contour, ContourVersion, GisImport, LayerFeature


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


# --- Bounded reports (review finding 2) --------------------------------------
#
# The cap is lowered rather than fed a real million-row file: `_map_features`,
# `_write_batch` and `_finish` all read `importer.MAX_REPORT_ROWS` at call time,
# so a small value exercises exactly the production code path in milliseconds.
# `test_import_parser.py` covers the same bound at its real value.

SMALL_CAP = 5


async def test_the_stored_error_report_is_capped_and_says_what_was_omitted(
    db, session_factory, pending_import_many_missing_numbers, monkeypatch
):
    monkeypatch.setattr(importer, "MAX_REPORT_ROWS", SMALL_CAP)
    await import_service.process_pending(session_factory)
    await db.refresh(pending_import_many_missing_numbers)

    report = pending_import_many_missing_numbers.error_report
    assert pending_import_many_missing_numbers.status == "failed"
    assert len(report) == SMALL_CAP + 1  # the cap plus the marker
    assert {e["code"] for e in report[:-1]} == {"missing_attribute"}
    assert report[-1]["code"] == "report_truncated"
    assert "7 further" in report[-1]["message"]  # 12 bad rows - 5 reported


async def test_the_warning_list_is_capped_too(
    db, session_factory, pending_import_many_duplicate_numbers, monkeypatch
):
    """Warnings do not fail a batch (ruling 7), so an unbounded warning list is
    the case that actually IMPORTS its way into a huge column — 151 features all
    mismatching on area is a plausible delivery, not a hostile one."""
    monkeypatch.setattr(importer, "MAX_REPORT_ROWS", SMALL_CAP)
    await import_service.process_pending(session_factory)
    await db.refresh(pending_import_many_duplicate_numbers)

    assert pending_import_many_duplicate_numbers.status == "review"  # still imported
    assert pending_import_many_duplicate_numbers.stats["created"] == 12
    warnings = pending_import_many_duplicate_numbers.stats["warnings"]
    assert len(warnings) == SMALL_CAP + 1
    assert {w["code"] for w in warnings[:-1]} == {"duplicate_number"}
    assert warnings[-1]["code"] == "report_truncated"
    assert warnings[-1]["omitted"] == 6  # 11 duplicates - 5 reported


# --- The crash path still audits and notifies (review finding 1) -------------


async def test_a_crashed_job_marks_the_row_and_still_audits_and_notifies(
    db, session_factory, pending_import, monkeypatch
):
    """Only OUR OWN defects reach `_mark_crashed` — which is exactly why the
    audit entry matters most here, and why the initiator must still be told:
    `_finish` notifies on a failed file, and a crash that stayed silent would
    strand them worse, since nothing else will ever speak."""
    from app.modules.audit.models import AuditLog
    from app.modules.notifications.models import Notification

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated defect inside run_import")

    monkeypatch.setattr(import_service, "run_import", boom)
    assert await import_service.process_pending(session_factory) == 1

    await db.refresh(pending_import)
    assert pending_import.status == "failed"
    assert pending_import.error_report[0]["code"] == "internal_error"
    assert pending_import.finished_at is not None

    entry = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.object_id == pending_import.id,
                    AuditLog.action == import_service.FINISH_ACTION,
                )
            )
        )
        .scalars()
        .one()
    )
    assert entry.user_id is None  # a job's own action
    assert entry.result == "error"
    assert entry.correlation_id.startswith("job:")
    assert entry.new_value["reason"] == "internal_error"

    notified = (
        (
            await db.execute(
                select(Notification).where(
                    Notification.object_id == pending_import.id,
                    Notification.event_code == import_service.IMPORT_EVENT,
                )
            )
        )
        .scalars()
        .all()
    )
    assert notified, "the initiator was left with no word that their import died"
    assert notified[0].recipient_user_id == pending_import.started_by


async def test_a_crash_with_no_initiator_still_audits(
    db, session_factory, pending_import, monkeypatch
):
    """A seeded/scripted import has nobody to notify; the audit entry is not
    optional for that reason."""
    from app.modules.audit.models import AuditLog

    pending_import.started_by = None
    await db.commit()

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated defect inside run_import")

    monkeypatch.setattr(import_service, "run_import", boom)
    await import_service.process_pending(session_factory)

    await db.refresh(pending_import)
    assert pending_import.status == "failed"
    entry = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.object_id == pending_import.id,
                    AuditLog.action == import_service.FINISH_ACTION,
                )
            )
        )
        .scalars()
        .one()
    )
    assert entry.user_id is None


# --- The warnings cap holds across producers (re-review finding) --------------
#
# `stats["warnings"]` is filled by TWO producers: `_write_batch` (duplicate
# number, area mismatch) and `_organization_warnings`, which runs afterwards.
# The first version capped only the first producer and computed the marker from
# its counter alone, so a later contribution could both push the list past the
# cap unnoticed AND make the marker under-state its own truncation. These call
# the real functions directly, no database — the arithmetic is the whole point.


def _org_mismatch_rows(count: int) -> list[import_service._Mapped]:
    """`count` features, each naming a DIFFERENT organization — what pointing
    `attribute_map`'s `organization_name` at a high-cardinality column (a tenant
    name, which the Agency's files carry) does in ordinary operation."""
    return [
        import_service._Mapped(row=i, wkb=b"", organization_name=f"Leshoz {i}")
        for i in range(count)
    ]


def test_a_later_producer_cannot_push_the_warning_list_past_the_cap(monkeypatch):
    """Failure mode 1: `_write_batch` stayed under the cap, so its counter was 0,
    and the old code returned the whole combined list untouched — no truncation
    and no marker, however many warnings the second producer added."""
    monkeypatch.setattr(importer, "MAX_REPORT_ROWS", SMALL_CAP)
    org_warnings, dropped_org = import_service._organization_warnings(
        _org_mismatch_rows(SMALL_CAP + 7), {"somewhere else"}
    )
    combined = import_service._truncate(
        list(org_warnings), dropped=0 + dropped_org, marker=import_service._warning_truncated
    )
    assert len(combined) == SMALL_CAP + 1
    assert combined[-1]["code"] == "report_truncated"
    assert combined[-1]["omitted"] == 7


def test_the_marker_counts_what_both_producers_dropped(monkeypatch):
    """Failure mode 2, the worse half: `_write_batch` already dropped 3 and a
    later producer added 10 past the cap. The old code silently erased all 10
    while claiming "3 further omitted" — a report that under-states its own
    truncation is more dangerous than one that does not truncate at all."""
    monkeypatch.setattr(importer, "MAX_REPORT_ROWS", SMALL_CAP)
    from_write_batch = [
        {"row": i, "code": "duplicate_number"} for i in range(SMALL_CAP)
    ]  # already at the cap
    dropped_by_write_batch = 3
    org_warnings, dropped_org = import_service._organization_warnings(
        _org_mismatch_rows(10), {"somewhere else"}
    )
    combined = import_service._truncate(
        [*from_write_batch, *org_warnings],
        dropped=dropped_by_write_batch + dropped_org,
        marker=import_service._warning_truncated,
    )
    assert len(combined) == SMALL_CAP + 1
    # 3 refused by _write_batch + 10 org warnings that no longer fit = 13, and
    # not one entry of the organization_mismatch category vanishes unaccounted.
    assert combined[-1]["omitted"] == 13
    assert {w["code"] for w in combined[:-1]} == {"duplicate_number"}


def test_the_organization_producer_bounds_its_own_accumulation(monkeypatch):
    """Memory, not just the column: de-duplicating by name is not a bound when
    every row carries a distinct name."""
    monkeypatch.setattr(importer, "MAX_REPORT_ROWS", SMALL_CAP)
    warnings, dropped = import_service._organization_warnings(
        _org_mismatch_rows(SMALL_CAP + 4), {"somewhere else"}
    )
    assert len(warnings) == SMALL_CAP
    assert dropped == 4


async def test_organization_mismatch_warnings_survive_end_to_end_and_are_capped(
    db, session_factory, pending_import_many_org_mismatches, monkeypatch
):
    """The same arithmetic through the real job, and the first test anywhere to
    exercise `organization_mismatch` at all: `organization_id` comes from the
    REQUEST and the file's own leshoz name is only COMPARED (ruling 12), so a
    disagreement warns and the batch still imports."""
    monkeypatch.setattr(importer, "MAX_REPORT_ROWS", SMALL_CAP)
    await import_service.process_pending(session_factory)
    await db.refresh(pending_import_many_org_mismatches)

    assert pending_import_many_org_mismatches.status == "review"  # a warning, not an error
    assert pending_import_many_org_mismatches.stats["created"] == 8
    warnings = pending_import_many_org_mismatches.stats["warnings"]
    assert len(warnings) == SMALL_CAP + 1
    assert warnings[-1]["code"] == "report_truncated"
    # 8 distinct names, none matching the organization the request named: 5 fit
    # under the cap, 3 do not — and the marker says 3, not 0.
    assert warnings[-1]["omitted"] == 3
    assert {w["code"] for w in warnings[:-1]} == {"organization_mismatch"}
