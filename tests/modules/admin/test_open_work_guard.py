"""tz/04 С23: "удаление — только после передачи незавершённых дел другому
исполнителю". Findings F4 and F5 of `docs/plans/07.5-audit-findings.md` are
that no code implemented that sentence — `delete_user` checked nothing at
all, and `archive_organization` checked only for active CHILD organizations.

Task 1's three tests use a FAKE provider rather than a real module: `admin` is
level 1 and must keep working without knowing who registers, and the fake
proves the registry's own MECHANISM (naming what is held, failing closed on a
provider's exception) independently of whatever real modules exist on this
branch. Task 2 adds `applications`' real provider on top of that mechanism.

**Track A scope (stage 07.6):** this branch registers ONLY `applications`'
provider (`app/event_subscriptions.py`) — `inspections` does not exist here
and is Track B's own module. The plan's inspection-task case
(`test_an_assigned_field_task_blocks_its_inspectors_deletion`) is therefore
NOT reproducible on this branch and is left to the integration pass, per the
stage 07.6 task assignment.
"""

import uuid

import pytest

from app.core.errors import DomainError
from app.modules.admin import open_work, service, users_service
from app.modules.auth.models import User
from tests.modules.admin.conftest import make_bare_application
from tests.modules.auth.test_sessions import make_user

# --- Task 1: the registry mechanism itself, proven with a fake provider -----


async def test_a_user_holding_nothing_is_deleted(db, staff_user: User, sys_admin: User):
    result = await users_service.delete_user(db, user_id=staff_user.id, actor=sys_admin)
    assert result.status == "deleted"


async def test_a_user_holding_open_work_is_refused_and_told_what_they_hold(
    db, staff_user: User, sys_admin: User
):
    held_id = uuid.uuid4()

    async def fake_provider(session, user_id):
        if user_id == staff_user.id:
            return open_work.OpenWork(kind="applications", count=1, ids=[held_id])
        return None

    open_work.OPEN_WORK_PROVIDERS.append(fake_provider)
    try:
        with pytest.raises(DomainError) as raised:
            await users_service.delete_user(db, user_id=staff_user.id, actor=sys_admin)
    finally:
        open_work.OPEN_WORK_PROVIDERS.remove(fake_provider)

    assert raised.value.code == "ERR-VAL-001"
    assert raised.value.details is not None
    # Naming what is held is the point: a refusal an admin cannot act on is a
    # dead end, and this project's defects hide information rather than leak it.
    assert raised.value.details["open_work"] == [
        {"kind": "applications", "count": 1, "ids": [str(held_id)]}
    ]
    await db.refresh(staff_user)
    assert staff_user.status == "active", "a refused delete must change nothing"


async def test_a_provider_that_raises_does_not_delete_the_user(
    db, staff_user: User, sys_admin: User
):
    """Fail closed. A provider that errors leaves the question unanswered, and
    deleting on an unanswered question is exactly the defect."""

    async def broken_provider(session, user_id):
        raise RuntimeError("boom")

    open_work.OPEN_WORK_PROVIDERS.append(broken_provider)
    try:
        with pytest.raises(RuntimeError):
            await users_service.delete_user(db, user_id=staff_user.id, actor=sys_admin)
    finally:
        open_work.OPEN_WORK_PROVIDERS.remove(broken_provider)

    await db.refresh(staff_user)
    assert staff_user.status == "active"


# --- Task 2 (applications half): the real provider --------------------------


async def test_an_application_under_review_blocks_its_reviewers_deletion(
    db, sys_admin: User, reviewer: User
):
    application = await make_bare_application(db, status="SUBMITTED", assigned_user_id=reviewer.id)

    with pytest.raises(DomainError) as raised:
        await users_service.delete_user(db, user_id=reviewer.id, actor=sys_admin)

    assert raised.value.details is not None
    kinds = {item["kind"]: item for item in raised.value.details["open_work"]}
    assert kinds["applications"]["count"] == 1
    assert str(application.id) in kinds["applications"]["ids"]
    await db.refresh(reviewer)
    assert reviewer.status == "active"


async def test_a_terminal_application_does_not_block(db, sys_admin: User, reviewer: User):
    """The guard must not become a reason nobody can ever be deleted: only work
    somebody must still act on counts (ruling R5). A long-serving reviewer with
    a CLOSED application on their name must still be deletable."""
    await make_bare_application(db, status="CLOSED", assigned_user_id=reviewer.id)

    result = await users_service.delete_user(db, user_id=reviewer.id, actor=sys_admin)
    assert result.status == "deleted"


# --- Task 3: archive_organization refuses while work hangs off it (F5) ------


async def test_archiving_a_leshoz_with_active_staff_is_refused(db, sys_admin: User, leshoz):
    await make_user(db, role_code="executor_staff", organization_id=leshoz.id)

    with pytest.raises(DomainError) as raised:
        await service.archive_organization(db, org_id=leshoz.id, actor=sys_admin)

    assert raised.value.code == "ERR-VAL-001"
    assert raised.value.details is not None
    assert raised.value.details["reason"] == "active users"
    await db.refresh(leshoz)
    assert leshoz.status == "active", "a refused archive must change nothing"


async def test_archiving_a_leshoz_with_an_open_application_is_refused(db, sys_admin: User, leshoz):
    application = await make_bare_application(db, status="SUBMITTED", assigned_org_id=leshoz.id)

    with pytest.raises(DomainError) as raised:
        await service.archive_organization(db, org_id=leshoz.id, actor=sys_admin)

    assert raised.value.details is not None
    assert raised.value.details["reason"] == "open applications"
    kinds = {item["kind"]: item for item in raised.value.details["open_work"]}
    assert str(application.id) in kinds["applications"]["ids"]
    await db.refresh(leshoz)
    assert leshoz.status == "active"


async def test_an_empty_leshoz_still_archives(db, sys_admin: User, leshoz):
    result = await service.archive_organization(db, org_id=leshoz.id, actor=sys_admin)
    assert result.status == "archived"
