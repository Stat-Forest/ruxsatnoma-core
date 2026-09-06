"""`oversight.service.harvest` — converting already-tagged `audit_log` rows
into `risk_indicators` rows. No fixture here builds a real business flow: the
tag is what other modules ALREADY write (grepped and catalogued in the plan),
so a bare `audit.service.log(..., extra=...)` call is the honest stand-in for
"some module raised this".

The shared test DB is persistent and, by the time this file runs as part of
the full suite, already carries real RI-tagged `audit_log` rows committed by
`applications`/`payments`/`permits`' own HTTP-driven tests (lesson: never
assume an empty database or an empty neighbourhood). Every test below drains
any such backlog with one `harvest()` call BEFORE creating its own row, so
the return value of the SECOND call is scoped to exactly what the test itself
added; row-level assertions key on the source `audit_log` row's own id
(`idempotency_key`), never on `code` alone."""

import uuid

from sqlalchemy import func, select

from app.modules.audit import service as audit
from app.modules.oversight import service
from app.modules.oversight.models import RiskIndicator


async def test_harvests_a_tagged_row_into_a_risk_indicator(db):
    await service.harvest(db)  # drain any pre-existing backlog first
    object_id = uuid.uuid4()
    entry = await audit.log(
        db,
        action="application.read",
        user_id=None,
        object_type="application",
        object_id=object_id,
        result="denied",
        basis="out_of_zone",
        extra={"risk_indicator": "RI-12"},
    )

    written = await service.harvest(db)

    assert written == 1
    row = (
        await db.execute(select(RiskIndicator).where(RiskIndicator.idempotency_key == entry.id))
    ).scalar_one()
    assert row.code == "RI-12"
    assert row.level == "high"
    assert row.object_type == "application"
    assert row.object_id == object_id
    assert row.details is not None
    assert row.details["basis"] == "out_of_zone"


async def test_harvest_is_idempotent_on_the_same_audit_row(db):
    await service.harvest(db)
    entry = await audit.log(
        db,
        action="invoice.reversal_record",
        object_type="invoice",
        object_id=uuid.uuid4(),
        extra={"risk_indicator": "RI-01"},
    )

    first = await service.harvest(db)
    second = await service.harvest(db)

    assert first == 1
    assert second == 0
    count = (
        await db.execute(
            select(func.count())
            .select_from(RiskIndicator)
            .where(RiskIndicator.idempotency_key == entry.id)
        )
    ).scalar_one()
    assert count == 1


async def test_harvest_detects_a_retroactive_publish_as_ri04(db):
    """`norms.service.publish_versioned` never tags `extra` — it stamps
    `new_value.retroactive = true` instead (`repo._RETROACTIVE_PUBLISH`)."""
    await service.harvest(db)
    entry = await audit.log(
        db,
        action="tariff.publish",
        object_type="tariff",
        object_id=uuid.uuid4(),
        new_value={"status": "published", "retroactive": True},
    )

    written = await service.harvest(db)

    assert written == 1
    row = (
        await db.execute(select(RiskIndicator).where(RiskIndicator.idempotency_key == entry.id))
    ).scalar_one()
    assert row.code == "RI-04"
    assert row.level == "high"
    assert row.object_type == "tariff"


async def test_harvest_ignores_an_untagged_row(db):
    await service.harvest(db)
    await audit.log(
        db, action="application.submit", object_type="application", object_id=uuid.uuid4()
    )

    assert await service.harvest(db) == 0


async def test_harvest_ignores_a_non_retroactive_publish(db):
    await service.harvest(db)
    await audit.log(
        db,
        action="tariff.publish",
        object_type="tariff",
        object_id=uuid.uuid4(),
        new_value={"status": "published", "retroactive": False},
    )

    assert await service.harvest(db) == 0
