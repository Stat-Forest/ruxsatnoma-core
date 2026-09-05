import uuid

from app.modules.applications.assignment import Candidate, choose_executor


def test_the_least_loaded_reviewer_wins() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    picked = choose_executor(
        [
            Candidate(user_id=a, open_count=4),
            Candidate(user_id=b, open_count=1),
            Candidate(user_id=c, open_count=7),
        ]
    )
    assert picked == b


def test_a_tie_breaks_by_user_id_so_the_choice_is_deterministic() -> None:
    """Ruling 7: a non-deterministic assignment is untestable and produces
    support tickets nobody can reproduce."""
    a, b = sorted([uuid.uuid4(), uuid.uuid4()])
    assert (
        choose_executor([Candidate(user_id=b, open_count=2), Candidate(user_id=a, open_count=2)])
        == a
    )


def test_no_eligible_reviewer_returns_none_rather_than_raising() -> None:
    """Ruling 7: the application is still assigned to the ORGANIZATION. It must
    never silently fail to assign, and it must not refuse the submission."""
    assert choose_executor([]) is None


async def test_submission_assigns_the_contours_organization_and_a_reviewer(
    db, applicant_client, draft_ready_for_submission, hodim_user, leshoz
) -> None:
    from tests.modules.applications.test_submit import _submit

    result = await _submit(applicant_client, draft_ready_for_submission)
    assert result.status_code == 200, result.text

    timeline = (
        await applicant_client.get(f"/api/v1/applications/{draft_ready_for_submission}/timeline")
    ).json()
    assert len(timeline["assignments"]) == 1
    assert timeline["assignments"][0]["org_id"] == str(leshoz.id)
    assert timeline["assignments"][0]["user_id"] == str(hodim_user.id)
    assert timeline["assignments"][0]["reason"] == "auto"


async def test_an_organization_with_no_reviewer_still_gets_the_application(
    db, applicant_client, draft_in_reviewerless_leshoz
) -> None:
    from tests.modules.applications.test_submit import _submit

    result = await _submit(applicant_client, draft_in_reviewerless_leshoz)
    assert result.status_code == 200, result.text

    timeline = (
        await applicant_client.get(f"/api/v1/applications/{draft_in_reviewerless_leshoz}/timeline")
    ).json()
    assert timeline["assignments"][0]["user_id"] is None


async def test_a_sys_admin_manual_reassignment_supersedes_the_automatic_one(
    sys_admin_client, submitted_application, other_hodim_user
) -> None:
    """Task 1 ANSWERED (б), 2026-09-05: `applications.assign` is granted to
    `sys_admin` alone — reassignment is an administrator's action, logged and
    rare, not the leshoz head's."""
    result = await sys_admin_client.post(
        f"/api/v1/applications/{submitted_application}/assign",
        json={"user_id": str(other_hodim_user.id), "reason": "absence"},
    )
    assert result.status_code == 200, result.text

    timeline = (
        await sys_admin_client.get(f"/api/v1/applications/{submitted_application}/timeline")
    ).json()
    active = [a for a in timeline["assignments"] if a["is_active"]]
    assert len(active) == 1, "the partial unique index allows exactly one"
    assert active[0]["user_id"] == str(other_hodim_user.id)


async def test_an_executor_head_may_not_reassign(
    executor_head_client, submitted_application, other_hodim_user
) -> None:
    """Task 1 ANSWERED (б): `applications.assign` is `sys_admin`-only
    (`migrations/versions/0015_applications.py`, `ROLE_GRANTS`) — the leshoz
    head has no grant for it, whatever `design/03` and ruling 13's own prose
    still say."""
    result = await executor_head_client.post(
        f"/api/v1/applications/{submitted_application}/assign",
        json={"user_id": str(other_hodim_user.id), "reason": "absence"},
    )
    assert result.status_code == 403


async def test_a_resubmission_does_not_re_fire_auto_assignment(
    db, applicant_client, submitted_application, hodim_user
) -> None:
    """Ruling 6: `submit`'s auto-assignment hook must fire only when the
    application has no active assignment row. Without the guard, a
    RETURNED-then-resubmitted application would run `choose_executor` again
    and the supersede branch (ruling 16.2) would silently hand it to a fresh
    pick, taking it from the reviewer who returned it. This drives the guard
    directly against `submit`, without going through Task 3's `/return`
    route (not yet built when this task runs) — Task 3's own resubmission
    test cross-references this one instead of repeating the assertion."""
    import uuid as _uuid

    from sqlalchemy import update

    from app.modules.applications.models import Application
    from tests.modules.applications.test_submit import _submit

    await db.execute(
        update(Application)
        .where(Application.id == _uuid.UUID(submitted_application))
        .values(status="RETURNED")
    )
    await db.commit()

    result = await _submit(applicant_client, submitted_application)
    assert result.status_code == 200, result.text

    timeline = (
        await applicant_client.get(f"/api/v1/applications/{submitted_application}/timeline")
    ).json()
    active = [a for a in timeline["assignments"] if a["is_active"]]
    assert len(active) == 1, "the guard must not insert a second row"
    assert active[0]["user_id"] == str(hodim_user.id), (
        "a resubmission must keep its existing reviewer, not re-run auto-assignment"
    )
