"""Taking an application into work, withdrawing it, and the timeline
(plan 03.9a task 6).

Three routes, one shared subject: `application_status_history`. `start-review`
and `cancel` each add a row to it, and `GET /timeline` is what makes those rows
readable — together with the `application_assignments` row `start-review`
writes and the ERI signature `submit` bound to the SUBMITTED row's own id
(ruling 25).

The timeline's signatures are TWO lookups, not one, and the failure mode of
getting it wrong is an EMPTY `signatures[]` that every assertion about statuses
and assignments still passes. That is why
`test_the_timeline_resolves_the_submission_signature_to_its_own_entry` is here
beside the brief's five: it is the only test in this file that would go red if
the submission lookup were dropped.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.auth.models import User


async def test_a_hodim_in_the_zone_takes_it_into_work(
    hodim_client, submitted_application: str
) -> None:
    result = await hodim_client.post(f"/api/v1/applications/{submitted_application}/start-review")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "IN_REVIEW"

    timeline = (
        await hodim_client.get(f"/api/v1/applications/{submitted_application}/timeline")
    ).json()
    assert [e["to_status"] for e in timeline["status_history"]] == [
        "DRAFT",
        "SUBMITTED",
        "IN_REVIEW",
    ]
    assert len(timeline["assignments"]) == 1


async def test_a_hodim_outside_the_zone_is_refused(
    other_zone_hodim_client, submitted_application: str
) -> None:
    """Ruling 14 grants this to any reviewer IN THE ZONE — the zone is the
    whole of the restriction, so it has to be tested at the HTTP level."""
    result = await other_zone_hodim_client.post(
        f"/api/v1/applications/{submitted_application}/start-review"
    )
    assert result.status_code in (403, 404)


async def test_taking_a_draft_into_work_is_refused(
    hodim_client, draft_ready_for_submission: str
) -> None:
    result = await hodim_client.post(
        f"/api/v1/applications/{draft_ready_for_submission}/start-review"
    )
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "ERR-APP-004"


async def test_the_applicant_cancels_an_application_already_in_review(
    applicant_client, hodim_client, submitted_application: str
) -> None:
    """tz/05 allows IN_REVIEW -> CANCELLED. An applicant who no longer wants the
    permit should not have to wait for a decision."""
    await hodim_client.post(f"/api/v1/applications/{submitted_application}/start-review")
    result = await applicant_client.post(
        f"/api/v1/applications/{submitted_application}/cancel",
        json={"reason": "changed my mind"},
    )
    assert result.status_code == 200
    assert result.json()["status"] == "CANCELLED"


async def test_a_cancelled_application_stops_blocking_a_new_one(
    applicant_client, submitted_application: str, second_draft_same_contour: str
) -> None:
    """The EXCLUDE constraint's WHERE clause excludes CANCELLED — worth an
    explicit test, because it is the reason the applicant can refile."""
    await applicant_client.post(f"/api/v1/applications/{submitted_application}/cancel", json={})
    from tests.modules.applications.test_submit import _submit

    result = await _submit(applicant_client, second_draft_same_contour)
    assert result.status_code == 200, result.text


# --- the half the five above cannot see (ruling 25) --------------------------


async def test_the_timeline_resolves_the_submission_signature_to_its_own_entry(
    applicant_client, submitted_application: str
) -> None:
    """The submission's ERI is bound to `("application_submission", <the
    SUBMITTED history row's id>)` — NOT to the application — so a timeline that
    looks the application up once comes back with an empty `signatures[]` and
    every other assertion in this file still passes.

    Three things are asserted, and each fails for its own reason: the signature
    is FOUND (the second lookup exists), it is attached to the SUBMITTED entry
    and to no other (the object_id is the history row's id, not the
    application's), and the top-level `signatures` — the DECISION line — is
    still empty, because nothing has decided this application. Task 7's
    forwarding test asserts exactly that last one.
    """
    timeline = (
        await applicant_client.get(f"/api/v1/applications/{submitted_application}/timeline")
    ).json()

    by_status = {entry["to_status"]: entry for entry in timeline["status_history"]}
    submitted = by_status["SUBMITTED"]
    assert len(submitted["signatures"]) == 1, "ruling 25's second lookup is missing"
    signature = submitted["signatures"][0]
    assert signature["purpose"] == "application_submit"
    assert signature["verification_status"] == "valid"
    # The id the signature is bound to IS this entry's own primary key — the
    # whole point of `submit` supplying it rather than letting `uuid7` default.
    assert uuid.UUID(submitted["id"])

    assert by_status["DRAFT"]["signatures"] == []
    assert timeline["signatures"] == [], "no decision signature exists yet"
    assert timeline["info_requests"] == [], "the key is a contract 3.9b widens"


async def test_the_out_of_zone_refusal_is_territorial_and_leaves_an_ri_12_trail(
    db: AsyncSession, other_zone_hodim_client, submitted_application: str
) -> None:
    """The brief's own zone test accepts 403 or 404 — and a 404 is also what a
    route that does not exist answers, so on its own it cannot say WHICH
    mechanism refused (lesson: "Two mechanisms refusing one thing: an
    outcome-only test cannot tell which one fired").

    The recorded reason can. `tz/10`'s RI-12 — «попытка доступа вне
    территориальных полномочий», High, immediate — is written under the flow
    verb's OWN action and COMMITTED before the refusal raises (decision #40
    ruling 2), so a denial that leaves no row is a denial that came from
    somewhere else.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from app.modules.applications.service import APPLICATION_START_REVIEW
    from app.modules.audit.models import AuditLog

    result = await other_zone_hodim_client.post(
        f"/api/v1/applications/{submitted_application}/start-review"
    )
    assert result.status_code == 404

    entry = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == APPLICATION_START_REVIEW,
                AuditLog.object_id == _uuid.UUID(submitted_application),
            )
        )
    ).scalar_one()
    assert entry.result == "denied"
    assert entry.basis == "out_of_zone"
    assert entry.extra == {"risk_indicator": "RI-12"}


def test_the_assignment_reason_is_one_the_check_constraint_allows() -> None:
    """One source of truth for an enum-ish column (lesson): `ASSIGNMENT_REASONS`
    is what `application_assignments`'s CHECK is built from, and a service
    constant that drifts off it is an `IntegrityError`/500 on the first
    start-review rather than a 422."""
    from app.modules.applications.models import ASSIGNMENT_REASONS
    from app.modules.applications.service import ASSIGNMENT_MANUAL

    assert ASSIGNMENT_MANUAL in ASSIGNMENT_REASONS


async def test_claiming_an_assignment_twice_supersedes_instead_of_colliding(
    db: AsyncSession,
    hodim_client,
    submitted_application: str,
    leshoz: Organization,
    staff_user: User,
) -> None:
    """`uq_application_assignments_active` is UNIQUE on `(application_id) WHERE
    is_active`, so a second insert beside a live row is an `IntegrityError` —
    and the deactivation must be FLUSHED before it, or both rows are pending
    when the index is checked and the insert fails on a conflict the flush would
    have resolved (lesson: "A partial unique index constrains only the rows it
    covers, and only after a flush").

    3.9a's own routes never reach that second claim — `start-review` runs once —
    so this calls the seam directly, the way `test_submit.py` calls
    `service._package_bytes`. Task 7's over-limit forward is its first real
    caller and asserts `len(timeline["assignments"]) == 2`; without the
    supersede written here that test would fail with a 500 in somebody else's
    task.

    No assertion on the ORDER of the two rows: the test session's transaction
    opened before the app's, and `created_at` is `now()` — transaction start
    time — so the row this test writes can legitimately carry the EARLIER
    timestamp. What must hold is that both survive and exactly one is active.
    """
    import uuid as _uuid

    from app.modules.applications import repo, service

    started = await hodim_client.post(f"/api/v1/applications/{submitted_application}/start-review")
    assert started.status_code == 200, started.text

    application = await repo.get_application(db, _uuid.UUID(submitted_application))
    assert application is not None
    second = await service._claim_assignment(
        db,
        application,
        org_id=leshoz.id,
        user_id=None,
        reason=service.ASSIGNMENT_MANUAL,
        actor=staff_user,
    )

    rows = await repo.list_assignments(db, application.id)
    assert len(rows) == 2, "the superseded row is kept — the register is a history"
    assert [row.id for row in rows if row.is_active] == [second.id]
