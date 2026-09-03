"""Who may bind a calculation to an application — the whole predicate
(`norms.service._calculable_statuses_for`, plan 03.9a task 5).

This guard stands where `CalculationIn.application_id`'s fail-closed type used
to. It decides **who may set the amount a citizen is invoiced**:
`payments.issue_invoice` and `permits.issue` each read the NEWEST calculation
for the application, independently, and being both level 4 they cannot compare
notes — so a row that slips past this function is billed and printed, and
`calculations` is append-only (migration 0011), so it can never be removed.

`POST /api/v1/calculations` requires `get_current_user` and **no permission
code at all**, which is why every case below is driven through that route: the
route is the attack surface, not the service.

Review round 2 found two whole classes of failure in exactly the branch nothing
covered, and each has a named regression test here:

  * **Critical 1** — `applications.view_any` (migration 0015 grants it to
    `prosecutor` alone, an oversight role with no write authority anywhere)
    admitted a READ-only auditor to a MONEY write;
  * **Critical 2** — the owner could re-price their own application while it
    sat in SUBMITTED, and `newest_calculation` is what an invoice bills.

Shared-test-DB discipline (lesson): every PINFL comes from a randomised
generator, never a literal — these fixtures COMMIT their users and
`users.pinfl` is unique.
"""

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, User, UserPermission
from app.modules.gis.models import Contour
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import _commit_pending_before_requests

pytestmark = pytest.mark.asyncio

API = "/api/v1"


def _pinfl() -> str:
    # Leading digit 9 — the other digits are claimed by sibling test modules
    # sharing this same persistent DB (the convention gis/conftest.py sets).
    return f"9{uuid.uuid4().int % 10**13:013d}"


async def _client_as(
    db: AsyncSession,
    *,
    role_code: str,
    organization_id: uuid.UUID | None = None,
    permissions: tuple[str, ...] = (),
) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in client holding a PRODUCTION role.

    NOT `gis.conftest._client_for`, and the difference is the whole point of
    Critical 1's regression test: `_client_for` builds every actor as
    `executor_staff`, and migration 0015 grants `applications.review` to that
    ROLE — so a "prosecutor" built that way would hold the review code through
    `role_permissions` and sail past the gate under test, proving nothing
    (lesson: a fixture's permission list must mirror the PRODUCTION role's
    grants). `permits.conftest._signer_for` makes the same choice for the same
    reason.
    """
    user = await make_user(db, role_code=role_code, organization_id=organization_id)
    for code in permissions:
        db.add(UserPermission(user_id=user.id, permission_code=code))
    await db.flush()
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


@pytest.fixture
async def guarded_applicant(db: AsyncSession) -> Applicant:
    """The citizen whose application every test below aims at."""
    user = await make_user(db, role_code="applicant", pinfl=_pinfl())
    row = Applicant(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def guarded_application(
    db: AsyncSession, guarded_applicant: Applicant, published_contour: Contour
) -> Application:
    """A DRAFT on `published_contour` — whose owner is the `leshoz` fixture, so
    "in zone" and "out of zone" below are facts about that shared organization
    rather than coincidences. `assigned_org_id` is left NULL, which is the
    state every application is in until a reviewer takes it into work: the
    zone must therefore resolve through the CONTOUR, the fallback branch
    nothing exercised before."""
    row = Application(
        applicant_id=guarded_applicant.id,
        submitted_by_user_id=guarded_applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
        status="DRAFT",
        contour_id=published_contour.id,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def owner_client(db: AsyncSession, guarded_applicant: Applicant):
    """The applicant who owns `guarded_application`, signed in."""
    user = await db.get(User, guarded_applicant.owner_user_id)
    assert user is not None
    _, token, csrf = await make_session(db, user)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        yield client


@pytest.fixture
async def prosecutor_client(db: AsyncSession, leshoz: Organization):
    """`prosecutor`, zoned to the leshoz that owns the contour — migration 0015
    grants `applications.view_any` to this role and to no other."""
    async for client in _client_as(db, role_code="prosecutor", organization_id=leshoz.id):
        yield client


@pytest.fixture
async def reviewer_client(db: AsyncSession, leshoz: Organization):
    """The hodim: `executor_staff`, zoned to the leshoz, holding
    `applications.review` through its own ROLE (migration 0015) — nothing is
    granted personally here, on purpose."""
    async for client in _client_as(db, role_code="executor_staff", organization_id=leshoz.id):
        yield client


@pytest.fixture
async def other_zone_reviewer_client(db: AsyncSession, other_leshoz: Organization):
    """The same role, a DIFFERENT leshoz. What differs from `reviewer_client`
    is the zone and nothing else, so a case that passes for one and fails for
    the other can only be about territory."""
    async for client in _client_as(db, role_code="executor_staff", organization_id=other_leshoz.id):
        yield client


@pytest.fixture
async def superuser_client(db: AsyncSession):
    """`sys_admin`. It passes every PERMISSION gate in the system (decision #41
    ruling 2) and `_holds_one_of` lets it through for that reason — but
    `_is_entitled_reviewer` then hands it the REVIEWER's status set, never a
    blanket pass, so APPROVED-and-beyond is closed to it like everybody else.
    No organization, so the zone half is republic-wide too and cannot be what
    refuses it."""
    async for client in _client_as(db, role_code="sys_admin"):
        yield client


@pytest.fixture
async def national_reviewer_client(db: AsyncSession):
    """`executor_staff` with no zone at all — `Zone(None, None, None)`, which
    `abac` reads as republic-wide and which short-circuits the organization
    lookup entirely."""
    async for client in _client_as(db, role_code="executor_staff"):
        yield client


def _body(application: Application, haymaking_activity_id: uuid.UUID) -> dict:
    return {
        "application_id": str(application.id),
        "contour_id": str(application.contour_id),
        "activity_type_id": str(haymaking_activity_id),
        "period_from": "2026-06-01",
        "period_to": "2026-09-30",
        "quantity": "3",
    }


# --- Critical 1: a READ code must never gate a MONEY write -------------------


async def test_a_prosecutor_in_zone_cannot_bind_a_calculation(
    db: AsyncSession,
    prosecutor_client: httpx.AsyncClient,
    guarded_application: Application,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """**Critical 1's regression pin.** `applications.view_any` is an oversight
    read code — `prosecutor` has no write authority anywhere in the system —
    and for one commit it admitted its holder to this route. A prosecutor whose
    zone covered the leshoz could POST a one-head calculation against a
    SUBMITTED application and have `issue_invoice` bill it and `issue` print
    it: a read-only auditor setting the fee.

    Both statuses are tried, because the defect was in the ENTITLEMENT branch
    and would show up whatever the status.
    """
    for status in ("DRAFT", "SUBMITTED"):
        guarded_application.status = status
        await db.flush()
        refused = await prosecutor_client.post(
            f"{API}/calculations", json=_body(guarded_application, haymaking_activity_id)
        )
        assert refused.status_code == 404, (status, refused.text)
        assert refused.json()["error"]["code"] == "ERR-SYS-003"


# --- Critical 2: the owner stops being the editor at SUBMITTED ---------------


async def test_the_owner_may_bind_in_draft_and_returned_and_nowhere_else(
    db: AsyncSession,
    owner_client: httpx.AsyncClient,
    guarded_application: Application,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """**Critical 2's regression pin.** `DRAFT` and `RETURNED` are the two
    states in which the applicant is the editor. `SUBMITTED`, `IN_REVIEW` and
    `PENDING_INFO` are not: an applicant who submits at 2 060 000,00 — signed,
    and bound to that submission — and then posts a one-head calculation while
    the filing sits in review would be INVOICED for the cheap row, since
    `issue_invoice` reads `newest_calculation`. `permits`' own
    `calculation_after_decision` defence cannot fire either, because the row
    predates the decision.

    The refusal reason is `application_not_editable_by_this_actor`, distinct
    from the everyone-is-refused `application_closed_for_calculation`: telling
    an applicant their SUBMITTED filing is "closed" would send them looking for
    a state change that never comes.
    """
    body = _body(guarded_application, haymaking_activity_id)

    for status in ("DRAFT", "RETURNED"):
        guarded_application.status = status
        await db.flush()
        allowed = await owner_client.post(f"{API}/calculations", json=body)
        assert allowed.status_code == 201, (status, allowed.text)

    for status in ("SUBMITTED", "IN_REVIEW", "PENDING_INFO"):
        guarded_application.status = status
        await db.flush()
        refused = await owner_client.post(f"{API}/calculations", json=body)
        assert refused.status_code == 409, (status, refused.text)
        error = refused.json()["error"]
        assert error["code"] == "ERR-NORM-005"
        assert error["details"]["reason"] == "application_not_editable_by_this_actor"
        assert error["details"]["status"] == status


async def test_a_row_planted_in_draft_is_not_what_an_invoice_would_bill(
    db: AsyncSession,
    owner_client: httpx.AsyncClient,
    guarded_application: Application,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """The other half of Critical 2's ruling, and the reason a speculative row
    in DRAFT needs no extra rule: whatever an applicant plants before
    submitting, the submission writes a NEWER row in a later transaction, and
    `repo.newest_calculation` orders `created_at DESC, id DESC` — so
    `applications.service.current_calculation`, which is what
    `payments.issue_invoice` and `permits.issue` both read, returns the
    submission's row and not the plant.

    Simulated at the seam rather than through `submit` (which needs the whole
    grazing/ERI apparatus): the plant goes through the REAL route, the later
    row is inserted the way `save_calculation` inserts it, and the assertion is
    on the function the two level-4 modules actually call.
    """
    from app.modules.applications import service as applications_service
    from app.modules.norms.calculator import RULE_CODE_VERSION
    from app.modules.norms.models import Calculation

    planted = await owner_client.post(
        f"{API}/calculations", json=_body(guarded_application, haymaking_activity_id)
    )
    assert planted.status_code == 201, planted.text

    later = Calculation(
        application_id=guarded_application.id,
        contour_id=guarded_application.contour_id,
        activity_type_id=haymaking_activity_id,
        rule_code_version=RULE_CODE_VERSION,
        input_snapshot={},
        amount=Decimal("2060000.00"),
        breakdown=[],
    )
    db.add(later)
    await db.flush()

    current = await applications_service.current_calculation(db, guarded_application.id)
    assert current is not None
    assert current.id == later.id, "the plant must never be the row an invoice is built from"
    assert str(current.id) != planted.json()["id"]


# --- Important 4: the staff branch, every path through it --------------------


async def test_a_reviewer_in_zone_may_bind_while_the_filing_is_under_review(
    db: AsyncSession,
    reviewer_client: httpx.AsyncClient,
    guarded_application: Application,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """3.9b's recalculation: a hodim holding `applications.review` corrects a
    herd or a period on a filing that is theirs to work on. The three review
    states are exactly what the owner may NOT reach, which is what makes the
    guard actor-dependent rather than one flat status set."""
    body = _body(guarded_application, haymaking_activity_id)
    for status in ("SUBMITTED", "IN_REVIEW", "PENDING_INFO"):
        guarded_application.status = status
        await db.flush()
        allowed = await reviewer_client.post(f"{API}/calculations", json=body)
        assert allowed.status_code == 201, (status, allowed.text)


async def test_a_reviewer_out_of_zone_is_refused_though_the_code_is_held(
    db: AsyncSession,
    other_zone_reviewer_client: httpx.AsyncClient,
    guarded_application: Application,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """Zone scoping is not a permission check, and neither is a substitute for
    the other (lesson). This actor holds `applications.review` by role and is
    still refused, because the contour belongs to another leshoz — 404, the
    same answer an id that never existed gets, never 403."""
    guarded_application.status = "SUBMITTED"
    await db.flush()
    refused = await other_zone_reviewer_client.post(
        f"{API}/calculations", json=_body(guarded_application, haymaking_activity_id)
    )
    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_national_reviewer_needs_no_organization_lookup(
    db: AsyncSession,
    national_reviewer_client: httpx.AsyncClient,
    guarded_application: Application,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """`Zone(None, None, None)` is republic-wide and short-circuits before the
    organization is resolved at all — the branch a zoned actor never reaches,
    and the one that would silently admit everybody if the short-circuit were
    ever moved below the lookup."""
    guarded_application.status = "IN_REVIEW"
    await db.flush()
    allowed = await national_reviewer_client.post(
        f"{API}/calculations", json=_body(guarded_application, haymaking_activity_id)
    )
    assert allowed.status_code == 201, allowed.text


async def test_assigned_org_id_wins_over_the_contours_owner(
    db: AsyncSession,
    reviewer_client: httpx.AsyncClient,
    other_zone_reviewer_client: httpx.AsyncClient,
    guarded_application: Application,
    other_leshoz: Organization,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """`assigned_org_id` is NULL until a reviewer takes the filing into work,
    so the zone resolves through the CONTOUR first — and flips the moment the
    application is assigned elsewhere. Both directions, because a rule that
    reads only one of the two columns passes half of this test either way."""
    guarded_application.status = "SUBMITTED"
    await db.flush()
    body = _body(guarded_application, haymaking_activity_id)

    # Unassigned: the contour's own leshoz decides.
    assert (await reviewer_client.post(f"{API}/calculations", json=body)).status_code == 201
    assert (
        await other_zone_reviewer_client.post(f"{API}/calculations", json=body)
    ).status_code == 404

    # Assigned away: the assignment decides, and the two swap places.
    guarded_application.assigned_org_id = other_leshoz.id
    await db.flush()
    assert (await reviewer_client.post(f"{API}/calculations", json=body)).status_code == 404
    assert (
        await other_zone_reviewer_client.post(f"{API}/calculations", json=body)
    ).status_code == 201


async def test_an_application_with_no_organization_at_all_fails_closed_for_a_zoned_actor(
    db: AsyncSession,
    reviewer_client: httpx.AsyncClient,
    guarded_application: Application,
    haymaking_activity_id: uuid.UUID,
    published_contour: Contour,
) -> None:
    """A draft with no contour and no assignment has no organization to
    compare a zone against. It is outside every ZONED actor's zone — refused,
    not admitted by a `None == None` accident."""
    guarded_application.status = "SUBMITTED"
    guarded_application.contour_id = None
    await db.flush()
    refused = await reviewer_client.post(
        f"{API}/calculations",
        json={
            "application_id": str(guarded_application.id),
            "contour_id": str(published_contour.id),
            "activity_type_id": str(haymaking_activity_id),
            "period_from": "2026-06-01",
            "period_to": "2026-09-30",
            "quantity": "3",
        },
    )
    assert refused.status_code == 404, refused.text


async def test_approved_and_beyond_is_refused_for_the_reviewer_too(
    db: AsyncSession,
    reviewer_client: httpx.AsyncClient,
    guarded_application: Application,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """The closed set is closed to EVERYONE, staff included: from `APPROVED`
    on, 3.10a's subscriber has issued the invoice inside the approval's own
    transaction and a price has been billed."""
    body = _body(guarded_application, haymaking_activity_id)
    for status in ("APPROVED", "INVOICED", "PAID", "PERMIT_ISSUED", "REJECTED", "ARCHIVED"):
        guarded_application.status = status
        await db.flush()
        refused = await reviewer_client.post(f"{API}/calculations", json=body)
        assert refused.status_code == 409, (status, refused.text)
        error = refused.json()["error"]
        assert error["code"] == "ERR-NORM-005"
        assert error["details"]["reason"] == "application_closed_for_calculation"


async def test_approved_and_beyond_is_refused_for_the_superuser_too(
    db: AsyncSession,
    superuser_client: httpx.AsyncClient,
    guarded_application: Application,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """**The one place in this branch where `sys_admin` is deliberately NOT let
    through a money write, stated rather than assumed** (final review).

    `_is_entitled_reviewer`'s superuser bypass is a PERMISSION bypass:
    `_holds_one_of` returns True for `sys_admin` without reading its grants,
    and `_calculable_statuses_for` then returns the REVIEWER's status set —
    not an unconditional pass. So the superuser may re-price a filing under
    review, and may NOT price one that has already been billed. Without a test
    saying so, a deliberate refusal and a forgotten branch look identical to
    whoever next simplifies this function.

    The IN_REVIEW pass at the end is what proves the refusal is about the
    STATUS and not about `sys_admin` being locked out of the route entirely.
    """
    body = _body(guarded_application, haymaking_activity_id)
    for status in ("APPROVED", "INVOICED", "PAID", "PERMIT_ISSUED", "REJECTED", "ARCHIVED"):
        guarded_application.status = status
        await db.flush()
        refused = await superuser_client.post(f"{API}/calculations", json=body)
        assert refused.status_code == 409, (status, refused.text)
        error = refused.json()["error"]
        assert error["code"] == "ERR-NORM-005"
        assert error["details"]["reason"] == "application_closed_for_calculation"

    guarded_application.status = "IN_REVIEW"
    await db.flush()
    allowed = await superuser_client.post(f"{API}/calculations", json=body)
    assert allowed.status_code == 201, allowed.text


async def test_the_guard_agrees_with_applications_own_vocabulary() -> None:
    """`norms` is level 2 and may not import `applications`, so `norms.service`
    re-declares two things that BELONG to `applications`: the permission codes
    that make a staffer a reviewer, and which statuses may still receive a
    calculation. A test is not bound by the module boundary, so this is where
    the copies are held to the originals.

    **`applications.view_any` must NOT be among them** — that was Critical 1,
    and this is the assertion that stops it coming back under a rename.
    """
    from app.modules.applications.models import APPLICATION_STATUSES
    from app.modules.applications.permissions import (
        APPLICATIONS_DECIDE,
        APPLICATIONS_REVIEW,
        APPLICATIONS_VIEW_ANY,
    )
    from app.modules.norms import service

    assert service._APPLICATION_RECALCULATE_CODES == {APPLICATIONS_REVIEW, APPLICATIONS_DECIDE}
    assert APPLICATIONS_VIEW_ANY not in service._APPLICATION_RECALCULATE_CODES

    # Ruling 11's READ set, added by task 8, and the one place the two sets are
    # held apart: `view_any` belongs in the read set — a prosecutor exists to
    # look — and must never be in the write one. It is the same three codes
    # `applications.service._holds_staff_read` accepts, which is what makes
    # "may read the calculation" mean the same thing as "may read the
    # application it prices".
    assert service._APPLICATION_READ_CODES == {
        APPLICATIONS_VIEW_ANY,
        APPLICATIONS_REVIEW,
        APPLICATIONS_DECIDE,
    }
    assert service._APPLICATION_RECALCULATE_CODES < service._APPLICATION_READ_CODES, (
        "anyone entitled to re-price an application is entitled to read its price"
    )

    owner = service._OWNER_CALCULABLE_STATUSES
    reviewer = service._REVIEWER_CALCULABLE_STATUSES
    closed = service._APPLICATION_CLOSED_FOR_CALCULATION

    assert owner == {"DRAFT", "RETURNED"}
    assert owner < reviewer, "a reviewer may do everything the owner may, and three states more"
    assert reviewer & closed == frozenset()
    assert reviewer | closed == set(APPLICATION_STATUSES)
    # `submit` writes its calculation at step 9, while the status is still
    # DRAFT (the SUBMITTED write is step 11) — so DRAFT being in the OWNER set
    # is what keeps the whole submission path working.
    assert "DRAFT" in owner


# --- Final review, Important 2: WHICH PLOT the calculation prices ------------


@pytest.fixture
async def another_published_contour(db: AsyncSession, contours_layer, leshoz, approval_doc):
    """A SECOND published contour in the SAME leshoz as `published_contour`.

    The same zone on purpose: the reviewer below is entitled to the application
    and entitled to the contour, so the refusal can only be about the two not
    describing each other.
    """
    from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt

    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    await db.flush()
    return contour


async def test_a_reviewer_cannot_bind_a_calculation_for_a_different_contour(
    db: AsyncSession,
    reviewer_client: httpx.AsyncClient,
    guarded_application: Application,
    another_published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """**WHO and WHEN are not enough — the row must describe THIS filing.**

    Everything in `CalculationIn` except `application_id` comes from the request
    body, so an in-zone hodim or head (both hold `applications.review`, and this
    route requires no permission code at all) could price a cheap contour and
    bind it to any application in their zone under review. It becomes the newest
    row, `payments.issue_invoice` bills it, and `permits.service.issue` then
    refuses the permit outright (`calculation_for_another_subject`): the citizen
    pays for a plot they never named and can never be issued a document, with
    the row frozen in an append-only table.

    The actor here is otherwise entitled on every axis the earlier tests cover —
    the code, the zone and the status all pass — which is what makes this a
    test of the subject check and of nothing else.
    """
    guarded_application.status = "IN_REVIEW"
    await db.flush()
    refused = await reviewer_client.post(
        f"{API}/calculations",
        json={
            "application_id": str(guarded_application.id),
            "contour_id": str(another_published_contour.id),
            "activity_type_id": str(haymaking_activity_id),
            "period_from": "2026-06-01",
            "period_to": "2026-09-30",
            "quantity": "3",
        },
    )
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "ERR-NORM-005"
    assert error["details"]["reason"] == "calculation_for_another_contour"

    from sqlalchemy import func, select

    from app.modules.norms.models import Calculation

    assert (
        await db.scalar(
            select(func.count())
            .select_from(Calculation)
            .where(Calculation.application_id == guarded_application.id)
        )
        == 0
    ), "nothing may be written: `calculations` is append-only (migration 0011)"


async def test_the_application_s_own_contour_is_still_accepted(
    db: AsyncSession,
    reviewer_client: httpx.AsyncClient,
    guarded_application: Application,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """The other half: a re-price on the application's OWN contour is exactly
    what 3.9b's recalculation is, and the guard must not stand in its way."""
    guarded_application.status = "IN_REVIEW"
    await db.flush()
    allowed = await reviewer_client.post(
        f"{API}/calculations", json=_body(guarded_application, haymaking_activity_id)
    )
    assert allowed.status_code == 201, allowed.text
