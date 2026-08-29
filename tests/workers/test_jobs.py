"""Purge/expiry jobs: retention deletes and the representation-expiry flip+audit."""

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from app.db import make_session_factory
from app.modules.auth.models import Applicant
from app.workers.jobs import expire_representations, purge_stale_rows
from tests.modules.auth.test_sessions import make_user


async def test_purge_deletes_only_stale_rows(engine, db):
    # committed rows outlive this test (a separate session does the purge, so
    # setup must be visible to it) — unique hashes keep reruns against the same
    # long-lived dev Postgres from colliding with a prior run's survivor.
    old_hash = f"h-old-{uuid.uuid4().hex[:8]}"
    fresh_hash = f"h-fresh-{uuid.uuid4().hex[:8]}"
    old = datetime.now(UTC) - timedelta(days=60)
    fresh = datetime.now(UTC) + timedelta(minutes=5)
    await db.execute(
        text(
            "INSERT INTO otp_codes (id, code_hash, purpose, expires_at, attempts) VALUES "
            "(gen_random_uuid(), :old_hash, 'phone_verify', :old, 0),"
            "(gen_random_uuid(), :fresh_hash, 'phone_verify', :fresh, 0)"
        ),
        {"old_hash": old_hash, "fresh_hash": fresh_hash, "old": old, "fresh": fresh},
    )
    delivered_old = datetime.now(UTC) - timedelta(days=30)
    await db.execute(
        text(
            # attempts has no DB-level default (only the ORM-side default=0 in the
            # model, migration 0008) — a raw INSERT that omits it violates NOT NULL.
            "INSERT INTO outbox_messages (id, destination, payload, status, attempts, delivered_at)"
            " VALUES "
            "(gen_random_uuid(), 'x', '{}', 'delivered', 0, :d),"
            "(gen_random_uuid(), 'x', '{}', 'dead', 0, NULL)"  # dead rows are the DLQ — kept
        ),
        {"d": delivered_old},
    )
    await db.commit()

    counts = await purge_stale_rows(make_session_factory(engine))
    assert counts["otp_codes"] >= 1
    assert counts["outbox_messages"] >= 1

    left = (
        await db.execute(
            text("SELECT count(*) FROM outbox_messages WHERE status = 'dead' AND destination = 'x'")
        )
    ).scalar_one()
    assert left >= 1  # dead rows survived
    fresh_left = (
        await db.execute(
            text("SELECT count(*) FROM otp_codes WHERE code_hash = :h"), {"h": fresh_hash}
        )
    ).scalar_one()
    assert fresh_left == 1

    audited = (
        await db.execute(text("SELECT count(*) FROM audit_log WHERE action = 'purge.run'"))
    ).scalar_one()
    assert audited >= 1


def _unique_stir() -> str:
    # Same shape as tests/modules/auth/test_legal_applicants.py's unique_stir():
    # a fresh 9-digit stir per call so reruns against the shared dev Postgres
    # never collide on applicants.uq_applicants_stir.
    return f"9{uuid.uuid4().int % 10**8:08d}"


async def test_expire_representations_flips_and_audits(engine, db):
    # No make_applicant fixture exists at this level (checked tests/modules/auth/);
    # inline the applicant the way tests/modules/auth/test_applicant_models.py does,
    # and the representation row via raw SQL like the purge test above.
    user = await make_user(db)
    applicant = Applicant(kind="legal", stir=_unique_stir(), name="OOO Test")
    db.add(applicant)
    await db.flush()
    await db.execute(
        text(
            "INSERT INTO representations (id, applicant_id, user_id, basis, valid_from,"
            " valid_until, status) VALUES"
            " (gen_random_uuid(), :a, :u, 'director_registry', :vf, :vu, 'active')"
        ),
        {"a": applicant.id, "u": user.id, "vf": date(2026, 1, 1), "vu": date(2026, 1, 2)},
    )
    await db.commit()

    n = await expire_representations(make_session_factory(engine))
    assert n >= 1
    status = (
        await db.execute(
            text("SELECT status FROM representations WHERE user_id = :u"), {"u": user.id}
        )
    ).scalar_one()
    assert status == "expired"
    audited = (
        await db.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'representation.expire'")
        )
    ).scalar_one()
    assert audited >= 1
