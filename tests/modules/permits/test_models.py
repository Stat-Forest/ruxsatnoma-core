import asyncio
import re
import uuid
from datetime import date
from decimal import Decimal
from typing import get_args

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.db import make_session_factory
from app.modules.notifications import repo as notifications_repo
from app.modules.notifications.service import DEFAULT_CHANNELS, SMS_EVENT_CODES
from app.modules.permits import repo
from app.modules.permits.events import NOTIFIED_EVENT_CODES
from app.modules.permits.models import (
    PERMIT_STATUSES,
    Permit,
    PermitStatusHistory,
    PermitTemplate,
)
from app.modules.permits.schemas import PermitStatus


async def _permit(
    db, application, *, series="А", number: int | None = None, status="pending_signatures"
) -> Permit:
    """`number` defaults to the NEXT one the counter hands out, never a literal:
    `test_issue.py` issues real permits whose rows COMMIT, so numbers 1..N are
    permanently taken on this shared, persistent test DB and a hard-coded 1 fails
    on `uq_permits_series_number` (lesson). The allocation itself rolls back with
    the test, so the same number is free again for the next one."""
    if number is None:
        number = await repo.next_number(db, series)
        assert number is not None
    row = Permit(
        series=series,
        number=number,
        application_id=application.id,
        applicant_id=application.applicant_id,
        activity_type_id=application.activity_type_id,
        organization_id=application.assigned_org_id,
        contour_id=application.contour_id,
        contour_version_id=application.contour_version_id,
        area_ha=Decimal("12.5000"),
        period_from=date(2027, 5, 1),
        period_to=date(2027, 9, 30),
        amount=Decimal("2060000.00"),
        sb_load=Decimal("40.0000"),
        status=status,
        qr_token=f"tok-{uuid.uuid4().hex}",
        snapshot={},
    )
    db.add(row)
    await db.flush()
    return row


async def test_series_and_number_are_unique_together(db, paid_application, second_paid_application):
    """tz/05 invariant 2. The database is the guarantee, not the service."""
    taken = await _permit(db, paid_application)
    with pytest.raises(IntegrityError, match="uq_permits_series_number"):
        await _permit(db, second_paid_application, number=taken.number)


async def test_one_permit_per_application(db, paid_application):
    """design/02: application_id is unique — the relation is 1:1."""
    await _permit(db, paid_application)
    with pytest.raises(IntegrityError, match="application_id"):
        await _permit(db, paid_application)


async def test_status_history_cannot_be_updated(db, paid_application):
    """tz/05 invariant 6 and the append-only idiom of audit_log, calculations
    and application_status_history."""
    permit = await _permit(db, paid_application)
    row = PermitStatusHistory(
        permit_id=permit.id, from_status=None, to_status="pending_signatures", changed_by=None
    )
    db.add(row)
    await db.flush()
    row.to_status = "active"
    with pytest.raises(DBAPIError, match="append-only"):
        await db.flush()


async def test_the_counter_hands_out_each_number_once_under_a_real_race(engine: AsyncEngine):
    """Ruling 9 and `tz/05` invariant 2: gapless, unique series numbers. `UPDATE …
    RETURNING` under the row lock, never SELECT-then-UPDATE.

    **Two REAL sessions, actually racing** (`test_signatures.py`'s two-session test is
    the shape). The version this replaced inlined its own SQL, never called
    `repo.next_number`, and ran both statements sequentially on ONE session — where a
    SELECT-then-UPDATE implementation passes identically, because a single session
    cannot contend with itself. The second caller here blocks on the first's row lock
    until it commits, which is what makes the assertion discriminate.

    The first allocation COMMITS (there is no other way to release the lock) and the
    second is rolled back, so this consumes exactly one number from the shared,
    persistent test database — the same cost an issuance test already pays."""
    series = get_settings().permit_series
    factory = make_session_factory(engine)

    async with factory() as first, factory() as second:
        allocated = await repo.next_number(first, series)
        assert allocated is not None

        racer = asyncio.create_task(repo.next_number(second, series))
        await asyncio.sleep(0.5)
        assert not racer.done(), (
            "the second allocation must block on the first's row lock — without it"
            " both read the same last_number and one series number is handed out twice"
        )

        await first.commit()
        second_number = await asyncio.wait_for(racer, timeout=10)
        assert second_number == allocated + 1
        await second.rollback()


async def test_the_counter_refuses_a_series_it_has_no_row_for(db):
    """`next_number`'s own documented failure: the series is CYRILLIC А (U+0410), and
    a Latin A (U+0041) matches no row. The statement then reports success and returns
    nothing, which is how a permit would be written with no number at all — so the
    caller must be able to SEE it, and `None` is what it sees."""
    assert await repo.next_number(db, "A") is None  # noqa: RUF001 - Latin A on purpose


async def _template(db, activity_type_id, *, version: int, status="active") -> PermitTemplate:
    row = PermitTemplate(
        activity_type_id=activity_type_id,
        version=version,
        name={"uz_cyrl": f"Шаблон v{version}", "ru": f"Шаблон v{version}"},
        status=status,
        valid_from=date(2027, 1, 1),
    )
    db.add(row)
    await db.flush()
    return row


async def test_only_one_template_version_is_active_per_activity_type(db, haymaking_activity_id):
    """Review round 1: `uq(activity_type_id, version)` alone lets two ACTIVE rows
    exist, so Task 3's "the active template for this activity" lookup would return
    whichever row the plan order handed back and freeze the wrong `template_id` into
    the permit permanently. Every sibling versioned catalogue (notification_templates
    0009, contour_versions) pins this with the same partial unique index."""
    await _template(db, haymaking_activity_id, version=1)
    with pytest.raises(IntegrityError, match="uq_permit_templates_active"):
        await _template(db, haymaking_activity_id, version=2)


async def test_archiving_the_active_template_frees_the_slot_for_the_next_version(
    db, haymaking_activity_id
):
    """The supersede the docstring promises, in the ONE order that works: archive,
    `flush()`, then insert. Without the flush both statements are still pending when
    the partial index is checked and the insert raises on a conflict the flush would
    have resolved (lesson). Archived rows sit outside the index, so the superseded
    version stays readable — an issued permit's `template_id` still resolves."""
    first = await _template(db, haymaking_activity_id, version=1)

    first.status = "archived"
    await db.flush()
    second = await _template(db, haymaking_activity_id, version=2)

    # Read back from the DATABASE, not off the two objects this test just set the
    # status on itself — `expire_on_commit=False` means those attributes are whatever
    # Python last wrote there, so asserting on them examines nothing (final fix wave).
    stored = {
        row.version: row.status
        for row in (
            await db.execute(
                select(PermitTemplate).where(
                    PermitTemplate.activity_type_id == haymaking_activity_id
                )
            )
        ).scalars()
    }
    assert stored == {1: "archived", 2: "active"}
    assert second.id != first.id, "a supersede inserts a row, it does not rewrite one"


async def test_the_schema_literals_match_the_tuple_the_check_is_built_from() -> None:
    """The one guard against `schemas.PermitStatus` drifting from the tuple the
    CHECK is built from (lesson: an enum-ish column has ONE source of truth). The
    members have to be written out — pyright rejects a starred variable inside
    `Literal` — so a status added on one side and forgotten on the other would be
    a 422 that should have been a 201, or an IntegrityError 500 that should have
    been a 422.

    Renamed: it compares the Literal to the TUPLE, which is one hop short of the
    table. `test_the_status_check_in_the_database_holds_all_six_statuses` below is
    the other hop."""
    assert set(get_args(PermitStatus)) == set(PERMIT_STATUSES)


async def test_the_status_check_in_the_database_holds_all_six_statuses(db) -> None:
    """The hop the test above cannot make. `models.py` builds its `CheckConstraint`
    from `PERMIT_STATUSES` by f-string, but migration `0019` spells the six values
    out as a literal — and `compare_metadata` does not diff CHECK constraints, so the
    autogenerate guard test would not notice the two disagreeing.

    Read back from `pg_constraint` on the live database, which is the migration's
    output and nothing else. The six exist from day one on purpose: `suspended`,
    `revoked` and `archived` are 3.11b's and 4.7's, and the plan's point is that
    neither stage needs a migration to widen this."""
    definition = await db.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = 'ck_permits_status_valid'"
        )
    )
    assert definition is not None, "migration 0019 must have created this CHECK"
    in_database = set(re.findall(r"'([a-z_]+)'", definition))
    assert in_database == set(PERMIT_STATUSES)
    assert len(PERMIT_STATUSES) == 6


async def test_every_event_code_this_module_notifies_on_has_an_active_template(db) -> None:
    """Ruling 17, made mechanical. With no template, `notify()` writes a raw fallback
    string in-app and **sends nothing at all** by SMS or e-mail, silently, with one log
    line — while a test asserting "a notification row exists" still passes. So the set
    the module declares it sends is checked against what the migrations actually seeded,
    per channel: `permit.issued` from 0009, four from 0019, `permit.due` from 0020.

    `notifications.repo.get_active_template` rather than a raw query, so this asserts
    exactly what `notify()` will find — including the `status='active'` filter."""
    missing = [
        (code, channel)
        for code in NOTIFIED_EVENT_CODES
        for channel in DEFAULT_CHANNELS
        # Ruling #211: `sms` is seeded for `SMS_EVENT_CODES` alone.
        if not (channel == "sms" and code not in SMS_EVENT_CODES)
        and await notifications_repo.get_active_template(db, event_code=code, channel=channel)
        is None
    ]
    assert missing == []


async def test_two_issuances_for_one_contour_and_activity_cannot_run_at_once(engine: AsyncEngine):
    """Ruling #176's last gate has to SERIALISE, not merely re-check.

    `service.issue` re-runs the capacity check before producing a numbered
    document, but a check alone does not stop two approvals decided in the same
    second: both read the same "before" picture, both find room, both pass.
    `repo.lock_contour_activity` is what makes the second wait for the first,
    and this test is what proves the lock is real — the contract asked for it
    and the track that wrote the lock did not write it, so nothing had ever
    demonstrated that two callers actually contend.

    Two REAL sessions, the shape `test_the_counter_hands_out_each_number_once_
    under_a_real_race` above uses: one session takes the lock, the second must
    BLOCK on the same key until the first's transaction ends, and must then
    proceed. A per-transaction advisory lock is released by COMMIT or ROLLBACK
    with no unlock call, so the rollback below both ends the test cleanly and
    demonstrates that release.
    """
    contour_id = uuid.uuid4()
    activity_type_id = uuid.uuid4()
    factory = make_session_factory(engine)

    async with factory() as first, factory() as second:
        await repo.lock_contour_activity(first, contour_id, activity_type_id)

        racer = asyncio.create_task(
            repo.lock_contour_activity(second, contour_id, activity_type_id)
        )
        await asyncio.sleep(0.5)
        assert not racer.done(), (
            "the second issuance must block on the first's lock — without it both"
            " read the same free contour and both produce a permit over one plot"
        )

        await first.rollback()
        await asyncio.wait_for(racer, timeout=10)
        await second.rollback()


async def test_the_lock_does_not_hold_up_a_different_contour_or_activity(engine: AsyncEngine):
    """The other half, and the reason the key is one 64-bit hash of the PAIR
    rather than two independent halves: an issuance on a DIFFERENT contour, or
    on the same contour for a different activity, must not wait at all.

    Without this, a lock that serialised every issuance in the country would
    pass the test above just as convincingly."""
    contour_id = uuid.uuid4()
    other_contour_id = uuid.uuid4()
    activity_type_id = uuid.uuid4()
    other_activity_type_id = uuid.uuid4()
    factory = make_session_factory(engine)

    async with factory() as first, factory() as second, factory() as third:
        await repo.lock_contour_activity(first, contour_id, activity_type_id)

        # Same activity, another contour — and the same contour, another
        # activity. Both must complete while the first transaction still holds
        # its own lock.
        await asyncio.wait_for(
            repo.lock_contour_activity(second, other_contour_id, activity_type_id), timeout=10
        )
        await asyncio.wait_for(
            repo.lock_contour_activity(third, contour_id, other_activity_type_id), timeout=10
        )

        await first.rollback()
        await second.rollback()
        await third.rollback()
