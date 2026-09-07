"""tz/04 С23 (`docs/tz/04-scenarii.md`): "Пользователи: ... удаление — только
после передачи незавершённых дел другому исполнителю" (a user may only be
deleted once their unfinished cases are handed to another executor), and
organizations are archived rather than deleted, with the tree kept reachable.

`docs/plans/07.3-findings.md` walked С23's happy path (counters, filters,
credential handout, roles/organizations/classifiers screens) and explicitly
left "deletion-with-open-cases" unwalked. This file walks it: `admin.
users_service.delete_user` and `admin.service.archive_organization` carry no
open-work check at all, only `admin.users_service.archive_role`'s active-
holders guard does (already covered by `test_roles_admin.py::
test_archive_role_blocked_by_active_holder` — a correct negative control, not
repeated here). These tests reproduce the gap; fixing it is a separate
decision (07.5 audit, track B)."""

import uuid

from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.admin.permissions import ORGANIZATIONS_MANAGE
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, User
from app.modules.auth.permissions import USERS_MANAGE
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with
from tests.modules.auth.test_sessions import make_user

API = "/api/v1"


def _pinfl() -> str:
    return f"1{uuid.uuid4().int % 10**13:013d}"


async def _open_application_assigned_to(db, reviewer: User) -> Application:
    """A SUBMITTED application under active review, routed to `reviewer` via
    `assigned_user_id` — exactly the row `POST /applications/{id}/assign`
    (3.9b, sys_admin-only) exists to move off a departing reviewer. `contour_id`
    stays null on purpose: `ex_applications_no_duplicate` only applies to rows
    where it is set, so this minimal row needs no GIS fixture at all."""
    citizen = await make_user(db, role_code="applicant", pinfl=_pinfl())
    applicant = Applicant(
        kind="individual", pinfl=citizen.pinfl, name=citizen.full_name, owner_user_id=citizen.id
    )
    db.add(applicant)
    await db.flush()
    application = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=citizen.id,
        on_behalf="self",
        status="SUBMITTED",
        channel="portal",
        assigned_user_id=reviewer.id,
    )
    db.add(application)
    await db.flush()
    return application


async def test_delete_user_orphans_an_open_application_assignment(db):
    """DEFECT: tz/04 С23 requires deletion to wait for a handover; the actual
    route deletes unconditionally and leaves the application pointed at a
    user who can no longer act on it (sessions are revoked) or even be found
    through `assigned_user_id` by anyone reading the admin user list, since a
    deleted user is filtered out of it by default."""
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    reviewer = await make_user(db, role_code="executor_staff")
    await db.flush()
    application = await _open_application_assigned_to(db, reviewer)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/admin/users/{reviewer.id}/delete")

    # The spec's own words call for a refusal here ("только после передачи
    # незавершённых дел другому исполнителю"). The API returns success.
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "deleted"

    await db.refresh(application)
    assert application.status == "SUBMITTED"  # still open
    # Orphaned: the only "owner" of this open review is now a deleted account,
    # and nothing flagged it, reassigned it or even warned the caller.
    assert application.assigned_user_id == reviewer.id


async def test_archive_organization_succeeds_despite_active_staff_and_an_open_application(
    db, agency
):
    """DEFECT: `archive_organization` (`app/modules/admin/service.py`) checks
    only for active CHILD organizations (`test_archive_blocked_while_active_
    children_exist`, already covered). It reads nothing from `users` or
    `applications`, so a leshoz still staffed and still holding an open
    application archives exactly as cleanly as an empty one — and nothing
    downstream re-checks `organizations.status` either (`grep -rn
    "organization.status\\|Organization.status" app/modules/applications
    app/modules/permits` outside `admin` itself returns zero hits), so the
    staff keep working against a leshoz that no longer appears active in any
    listing that defaults to `status="active"` (`admin.repo.list_organizations`)."""
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, ORGANIZATIONS_MANAGE)
    leshoz = Organization(
        kind="leshoz",
        code=f"gap-{suffix}",
        name={"uz_cyrl": "Х", "uz_latn": "X"},
        parent_id=agency.id,
    )
    db.add(leshoz)
    await db.flush()

    staff = await make_user(db, role_code="executor_staff", organization_id=leshoz.id)
    await db.flush()
    application = await _open_application_assigned_to(db, staff)
    application.assigned_org_id = leshoz.id
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/admin/organizations/{leshoz.id}/archive")

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "archived"

    await db.refresh(staff)
    await db.refresh(application)
    assert staff.status == "active"  # still logs in, still zoned to this org
    assert staff.organization_id == leshoz.id
    assert application.status == "SUBMITTED"  # still open, routed to an archived org
    assert application.assigned_org_id == leshoz.id
