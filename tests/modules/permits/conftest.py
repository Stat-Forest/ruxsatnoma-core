"""Fixtures for the permits module.

Rows are built directly through the ORM, never through an API: `applications`
has no `router.py` on `dev` (3.9a shipped models/service/repo/events only), so
there is no route that creates an application to drive. The spatial primitives
come from `tests/modules/gis/conftest.py` as plain importables — the same idiom
`tests/modules/applications/conftest.py` and `tests/modules/norms/conftest.py`
already use; two ways to build a contour is how the two drift apart.

Each application gets its OWN applicant and its OWN contour. That is not
tidiness: `ex_applications_no_duplicate` (migration 0015) forbids two
applications for the same (applicant, contour, activity) on an overlapping
period whenever the status is one of the active ones — and `PAID` is one of
them — so two `PAID` fixtures sharing an applicant and a contour would fail at
insert, before the permit test they exist for ever ran.

Task 3 adds the module's first HTTP-driven tests, so `_app_on_test_db` lands
here too (lesson: without it `create_app()` opens the shared DEV database and
every request 401s with no hint that the database is the bug).
"""

import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import files, storage
from app.core.models import MediaFile, SystemSetting
from app.core.settings_store import invalidate
from app.main import create_app
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, User
from app.modules.gis.models import Contour, GisLayer
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.norms.calculator import RULE_CODE_VERSION
from app.modules.norms.models import Calculation
from app.modules.notifications.models import Notification
from app.modules.permits import decisions, grounds, repo, service, signers
from app.modules.permits.models import Permit, PermitTemplate
from app.modules.permits.permissions import PERMITS_ISSUE
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import (
    _client_for,
    _commit_pending_before_requests,
    applicant_client,  # noqa: F401 — a fixture imported into a conftest IS available
    make_contour,
    make_version,
    random_box_wkt,
)
from tests.modules.gis.conftest import approval_doc as approval_doc
from tests.modules.gis.conftest import contours_layer as contours_layer
from tests.modules.gis.conftest import leshoz as leshoz
from tests.modules.gis.conftest import other_leshoz as other_leshoz


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """The same guard `tests/modules/gis/conftest.py` and
    `tests/modules/norms/conftest.py` carry: the app under test must open the
    TEST database, not the dev one. An autouse fixture applies only inside its
    own package, so importing gis's helpers does NOT bring it along."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def unique_pinfl() -> str:
    """Leading digit 1 is what `tests/modules/applications/conftest.py` uses; the
    remaining 13 digits are random, so two modules sharing this persistent test
    DB never collide on `uq_users_pinfl`."""
    return f"1{uuid.uuid4().int % 10**13:013d}"


# The holder of every fixture application, in the form `tz/13` requisite 10 has:
# «Фамилия Исм Отасининг исми». The same name `test_render.py` renders, so the two
# halves of the document — what is printed and what the QR page shows masked —
# are read off one string. Its mask is С12's own example, «А.***ов А.».
HOLDER_NAME = "Азизов Азиз Азизович"

# The herd every grazing fixture is priced for, and — since ruling T3-f — the herd
# its permit PRINTS (`tz/13` requisites 12-15). One code from each of form 1-ilova's
# four rows, so a single issuance exercises all four; the conditional-head load adds
# up to the `used_sb=40.0000` the fixture carries (5x6.0 + 2x3.5 + 2x1.0 + 5x0.2),
# because a permit whose printed heads and printed SB load disagreed would be the
# very defect the frozen-snapshot ruling exists to prevent.
GRAZING_HERD: tuple[tuple[str, int], ...] = (
    ("cattle_adult", 5),
    ("horse_young", 2),
    ("sheep_goat_6m", 2),
    ("lamb_kid_under_6m", 5),
)


def calculation_input_snapshot(items: tuple[tuple[str, int], ...]) -> dict[str, object]:
    """The shape `norms.calculator.calculate` freezes into `calculations.input_snapshot`,
    reduced to the part issuance reads (`input_snapshot["request"]["items"]`, ruling
    T3-f). Built here rather than by running the real calculator: these fixtures insert
    `Calculation` rows directly, and a stub whose SHAPE drifts from the calculator's
    would let issuance pass against a snapshot no real calculation ever looks like —
    `calculator.from_input_snapshot` is the contract this mirrors.

    `items` is empty for every activity but grazing, where `quantity` carries the
    amount instead — which is why the four head-count rows must have a not-applicable
    form rather than four zeros."""
    return {
        "request": {
            "activity_code": "grazing" if items else "apiary",
            "items": [{"livestock_code": code, "count": count} for code, count in items],
            "quantity": None if items else "10",
        },
        "rule_code_version": RULE_CODE_VERSION,
    }


@pytest.fixture
async def grazing_activity_id(db: AsyncSession) -> uuid.UUID:
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'grazing'"))
    return rows.scalar_one()


@pytest.fixture
async def haymaking_activity_id(db: AsyncSession) -> uuid.UUID:
    """A second activity type, for the `permit_templates` uniqueness tests: migration
    0019 already seeds an ACTIVE grazing template, so a test that built its own v1
    there would collide with the seed rather than with the row it created."""
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'haymaking'"))
    return rows.scalar_one()


@pytest.fixture
async def apiary_activity_id(db: AsyncSession) -> uuid.UUID:
    """A THIRD activity type, for the stored-layout arm of the template lookup.
    Deliberately not `haymaking`: `test_models.py` inserts its own ACTIVE haymaking
    template and needs the slot empty, while the fixture below has to COMMIT its
    template for the app's own session to see it."""
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'apiary'"))
    return rows.scalar_one()


async def make_paid_application(
    db: AsyncSession,
    *,
    layer: GisLayer,
    org: Organization,
    approval_doc: MediaFile,
    activity_type_id: uuid.UUID,
    status: str = "PAID",
    used_sb: Decimal | None = Decimal("40.0000"),
    with_calculation: bool = True,
    items: tuple[tuple[str, int], ...] = GRAZING_HERD,
) -> Application:
    """An application in `PAID` — the only status this module issues a permit
    from (ruling 10) — with its own applicant, contour and published version, at
    a random, isolated spot (a fixed committed geometry accumulates across runs
    on the shared test DB — lesson).

    It also gets a `Calculation`, because issuance reads the priced amount out of
    `applications.service.current_calculation` and refuses without one (`tz/13`
    field 18 is not optional on a permit) — and, since ruling T3-f, the HERD too:
    `tz/13`'s head-count rows come from that same frozen `input_snapshot`, so the
    printed heads and the printed amount can never belong to different moments.
    `used_sb=None` with `items=()` is the shape of an activity that commits no
    conditional-head load at all — haymaking, apiaries — where the permit's
    `sb_load` stays null (task 1, decision 3).

    `with_calculation=False` is how a test reaches the "no calculation" refusal:
    `calculations` is append-only at the database level (migration 0011's trigger),
    so a row cannot be deleted afterwards — the application has to be built
    without one."""
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    # `tz/13` requisite 10, and since Task 5 the half of it the public QR card
    # shows masked. `make_user`'s default is "Test User", which no masking rule
    # can turn into С12's «А.***ов А.»; `HOLDER_NAME` is the same canonical name
    # `test_render.py` renders with. Assigned after the call rather than passed
    # to it: `make_user` already gives `User(...)` a `full_name`, so an override
    # of that key would raise `TypeError: got multiple values`.
    user.full_name = HOLDER_NAME
    applicant = Applicant(
        kind="individual",
        pinfl=user.pinfl,
        name=user.full_name,
        # `tz/13` requisite 11. Nullable in the registry and never a blocker
        # (ruling T3-g): `_holder_address` composes what the registry does hold and
        # prints `NOT_STATED` when it holds nothing, so an applicant with no address
        # is still issued a permit. Filled here so the happy-path snapshot carries a
        # real address; do NOT reinstate a `_required` on it — that would refuse a
        # citizen who has already paid.
        address="Тошкент вилояти, Бўстонлиқ тумани, Бурчмулла қишлоғи, 1-уй",
        owner_user_id=user.id,
    )
    db.add(applicant)
    await db.flush()

    contour = await make_contour(db, layer, org)
    version = await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )

    row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=user.id,
        on_behalf="self",
        activity_type_id=activity_type_id,
        contour_id=contour.id,
        contour_version_id=version.id,
        requested_area_ha=Decimal("12.5000"),
        period_from=date(2027, 5, 1),
        period_to=date(2027, 9, 30),
        status=status,
        channel="portal",
        assigned_org_id=org.id,
    )
    db.add(row)
    await db.flush()

    if not with_calculation:
        return row

    db.add(
        Calculation(
            application_id=row.id,
            contour_id=contour.id,
            activity_type_id=activity_type_id,
            rule_code_version=RULE_CODE_VERSION,
            input_snapshot=calculation_input_snapshot(items),
            used_sb=used_sb,
            amount=Decimal("2060000.00"),
            breakdown={"total": "2060000.00"},
        )
    )
    await db.flush()
    return row


@pytest.fixture
async def paid_application(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    grazing_activity_id: uuid.UUID,
) -> Application:
    return await make_paid_application(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
    )


@pytest.fixture
async def second_paid_application(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    grazing_activity_id: uuid.UUID,
) -> Application:
    """A second, fully independent application — the one a uniqueness test needs
    so the row it inserts collides on `(series, number)` and not on
    `permits.application_id`, which is unique too."""
    return await make_paid_application(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
    )


@pytest.fixture
async def approved_application(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    grazing_activity_id: uuid.UUID,
) -> Application:
    """An application that has NOT been paid. Built directly, never through the
    API: once 3.10a is merged its invoice subscriber moves an application from
    APPROVED to INVOICED inside the approval's own transaction (3.10a ruling 14),
    so APPROVED is unreachable through any endpoint. The refusal this fixture
    exists for holds for either status — do not rewrite it to go through
    `approve` on the assumption that it can."""
    return await make_paid_application(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
        status="APPROVED",
    )


@pytest.fixture
async def unpriced_application(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    grazing_activity_id: uuid.UUID,
) -> Application:
    """PAID, but with no `Calculation` — `tz/13` field 18 has no source. Built
    without one rather than stripped afterwards: `calculations` is append-only at
    the database level, so `DELETE FROM calculations` raises."""
    return await make_paid_application(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
        with_calculation=False,
    )


@pytest.fixture
async def apiary_paid_application(
    db: AsyncSession,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    apiary_activity_id: uuid.UUID,
) -> Application:
    """A paid application for the activity whose template carries a STORED layout.
    `used_sb=None` because an apiary commits no conditional-head load — which is
    also what makes the stored layout below legitimately narrower than the
    bundled one."""
    return await make_paid_application(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=apiary_activity_id,
        used_sb=None,
        items=(),
    )


@pytest.fixture
async def applicant_row(db: AsyncSession, paid_application: Application) -> Applicant:
    """The `Applicant` the permit's `holder_name`/`holder_pinfl` are copied from —
    looked up rather than returned by `make_paid_application`, so `test_models.py`'s
    own callers are untouched."""
    row = await db.get(Applicant, paid_application.applicant_id)
    assert row is not None
    return row


@pytest.fixture
async def applicant_user(db: AsyncSession, applicant_row: Applicant) -> User:
    """The individual applicant's own account (`applicants.owner_user_id`) — the
    recipient of the `permit.issued` notification."""
    assert applicant_row.owner_user_id is not None
    user = await db.get(User, applicant_row.owner_user_id)
    assert user is not None
    return user


@pytest.fixture
async def assigned_executor(
    db: AsyncSession, leshoz: Organization, paid_application: Application
) -> User:
    """The hodim the application is assigned to — who `payment_confirmed` tells
    that a permit is now due (ruling 19). `make_paid_application` leaves
    `assigned_user_id` null on purpose, so the "nobody assigned" arm has a fixture
    of its own: the plain `paid_application`."""
    user = await make_user(db, role_code="executor_staff", organization_id=leshoz.id)
    paid_application.assigned_user_id = user.id
    await db.flush()
    return user


# --- the stored-layout arm of the template lookup ----------------------------

STORED_LAYOUT = (
    "<html><body><h1>{{ series }} № {{ number }}</h1>"
    '<p>Асаларичилик — {{ holder_name }}</p><img src="{{ qr }}"></body></html>'
)


# A FIXED id and storage key, so the fixture is get-or-create rather than
# create-and-clean-up. It cannot clean up: the test issues a permit against this
# template, `permits.template_id` is an FK to it, and an issued permit's template
# must resolve forever — so a teardown DELETE raises ForeignKeyViolation, and the
# permit itself cannot be deleted either (`permit_status_history` is append-only
# at the database level). Idempotent setup is the only shape that survives a
# second run on this shared, persistent test DB.
APIARY_LAYOUT_FILE_ID = uuid.UUID("01a06200-0000-7000-8000-000000000001")
APIARY_LAYOUT_KEY = "t/permits-apiary-layout.html"


@pytest.fixture
async def apiary_template(
    db: AsyncSession, apiary_activity_id: uuid.UUID
) -> AsyncIterator[PermitTemplate]:
    """An ACTIVE template whose `layout_file_id` points at a stored HTML file —
    the arm migration 0019's seeded grazing row (null `layout_file_id`, meaning
    "the layout bundled in `assets/`") does not exercise.

    Committed, because the app runs on its own session and connection. Reused
    rather than recreated when a previous run left it behind — the same
    get-or-create shape `tests/modules/gis/conftest.py::leshoz` uses for the
    singleton agency row, and here it is not a nicety but the only option (see
    the constants above). The object is re-uploaded every time, so the stored
    bytes can never drift from `STORED_LAYOUT` the test compares against.
    """
    await storage.ensure_bucket()
    data = STORED_LAYOUT.encode("utf-8")
    await storage.put_object(APIARY_LAYOUT_KEY, data, "text/html")

    file = await db.get(MediaFile, APIARY_LAYOUT_FILE_ID)
    if file is None:
        file = MediaFile(
            id=APIARY_LAYOUT_FILE_ID,
            storage_key=APIARY_LAYOUT_KEY,
            filename="apiary_layout.html",
            content_type="text/html",
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        db.add(file)
        await db.flush()

    template = (
        await db.execute(
            select(PermitTemplate).where(
                PermitTemplate.activity_type_id == apiary_activity_id,
                PermitTemplate.status == "active",
            )
        )
    ).scalar_one_or_none()
    if template is None:
        template = PermitTemplate(
            activity_type_id=apiary_activity_id,
            version=1,
            name={"uz_cyrl": "Асаларичилик шакли", "ru": "Форма для пасек"},
            layout_file_id=file.id,
            status="active",
            valid_from=date(2027, 1, 1),
        )
        db.add(template)
    # Self-healing rather than asserting: this row is the fixture's OWN, left
    # behind by a previous run, and an earlier version of this fixture pointed it
    # at a randomly-named file. Repointing it keeps the one active apiary template
    # consistent with the constants above, which is what the test compares against.
    template.layout_file_id = file.id
    await db.commit()
    yield template


# --- clients -----------------------------------------------------------------
# `_client_for(db, *permissions, organization_id=None)` is an async generator
# (gis/conftest.py): drive it with `async for`. Permissions are POSITIONAL.


@pytest.fixture
async def hodim_client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """The «ходим» who forms the document — `permits.issue`, which migration 0019
    grants to `executor_staff`, the very role `_client_for` builds. Zone-free, so
    the happy path is not also a zone test."""
    async for client in _client_for(db, PERMITS_ISSUE):
        yield client


@pytest.fixture
async def other_zone_hodim_client(
    db: AsyncSession, other_leshoz: Organization
) -> AsyncIterator[httpx.AsyncClient]:
    """A `permits.issue` holder zoned to a DIFFERENT leshoz — zone scoping is not
    a permission check (lesson), and issuance is a write path that needs both."""
    async for client in _client_for(db, PERMITS_ISSUE, organization_id=other_leshoz.id):
        yield client


# `applicant_client` (re-exported above) is what the permission-denial test uses.
# A grantless `_client_for(db)` would NOT do: it builds an `executor_staff` user,
# and migration 0019 grants `permits.issue` to that very ROLE — the actor would
# hold the code through `role_permissions` and sail past the route's dependency
# (lesson: a fixture's permission list must mirror the production role's grants).


# --- Task 4: the 3+1 signatories ---------------------------------------------


@dataclass(frozen=True)
class Signer:
    """A signed-in client PLUS the ERI identity that client signs with.

    The identity travels with the client instead of being written as a literal
    at each call site, and it is generated fresh per test. Literals cannot
    survive a second run on this shared, persistent database: these fixtures
    COMMIT their user, `users.pinfl` is UNIQUE, and `certificates` is unique on
    `(serial_number, issuer)` — so run two fails on `uq_users_pinfl`, and a
    certificate already bound to run one's user is refused outright by
    `bind_certificate` as `certificate_owned_by_another_user` (lesson: the test
    DB is shared, persistent and never empty — including the spot you picked).
    """

    client: httpx.AsyncClient
    user: User
    pinfl: str
    serial: str
    # 3.11b: `sign_decision` (below) needs a session to read the permit and the
    # ground BEFORE it can build the bytes it signs — the same `db` the test
    # itself holds, carried on the `Signer` so a helper taking only `signer` can
    # reach it (`_db_of`).
    db: AsyncSession


async def _signer_for(
    db: AsyncSession,
    *,
    role_code: str,
    organization_id: uuid.UUID | None = None,
    user: User | None = None,
) -> AsyncIterator[Signer]:
    """A signed-in `Signer` holding a PRODUCTION role, not `_client_for`'s
    `executor_staff` stand-in.

    `_client_for` builds every actor as `executor_staff` and hands it personal
    grants. That is exactly wrong for this task: the check under test reads the
    actor's ROLE, so an `executor_staff` user with `permits.sign` bolted on
    would prove something about a role that does not exist. Building the user
    under the real role also means the grants arrive the way they do in
    production — migration 0019 gives `permits.sign` to `executor_head`,
    `chief_forester`, `accountant` and `applicant` — so no fixture below lists a
    permission at all: it inherits the role's own `role_permissions` rather than
    restating them (lesson: a fixture's permission list must mirror the
    PRODUCTION role's grants).

    `user=` reuses an account that already exists (the holder, who must be the
    applicant of the permit's own application, not a fresh stranger).
    """
    if user is None:
        user = await make_user(
            db, role_code=role_code, organization_id=organization_id, pinfl=unique_pinfl()
        )
    _, token, csrf = await make_session(db, user)
    await db.commit()
    assert user.pinfl is not None
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        # The serial is derived from the pinfl, so one signer always presents the
        # same certificate and two never collide on `uq_certificate_identity`.
        yield Signer(client=client, user=user, pinfl=user.pinfl, serial=f"SER-{user.pinfl}", db=db)


def _db_of(signer: Signer) -> AsyncSession:
    """The session a `sign_decision` caller reads through — trivial, but named
    so every call site says WHY a `Signer` carries a database session at all."""
    return signer.db


@pytest.fixture
async def head_client(db: AsyncSession, leshoz: Organization) -> AsyncIterator[Signer]:
    """«Раҳбар» — the leshoz head, `executor_head` (never `rahbar`, which is not a
    `roles.code` at all: lesson). Zoned to `leshoz`, which is the organization
    every fixture application's contour belongs to, so this is the signatory the
    permit actually names."""
    async for signer in _signer_for(db, role_code="executor_head", organization_id=leshoz.id):
        yield signer


@pytest.fixture
async def chief_forester_client(db: AsyncSession, leshoz: Organization) -> AsyncIterator[Signer]:
    """The second signature line of `tz/13`. `chief_forester` exists for this and
    no other purpose (decision #32)."""
    async for signer in _signer_for(db, role_code="chief_forester", organization_id=leshoz.id):
        yield signer


@pytest.fixture
async def accountant_client(db: AsyncSession, leshoz: Organization) -> AsyncIterator[Signer]:
    """The third signature line — «Бухгалтер»."""
    async for signer in _signer_for(db, role_code="accountant", organization_id=leshoz.id):
        yield signer


@pytest.fixture
async def other_org_head_client(
    db: AsyncSession, other_leshoz: Organization
) -> AsyncIterator[Signer]:
    """An `executor_head` of a DIFFERENT leshoz: holds the role and the
    `permits.sign` grant that comes with it, and still may not sign this
    permit (lesson: zone scoping is not a permission check)."""
    async for signer in _signer_for(db, role_code="executor_head", organization_id=other_leshoz.id):
        yield signer


@pytest.fixture
async def sys_admin_client(db: AsyncSession) -> AsyncIterator[Signer]:
    """The superuser. `require_permission` waves it through every gate
    (decision #41 ruling 2) — a signatory check must NOT wave it through, because
    a signature is an identity, not a privilege."""
    async for signer in _signer_for(db, role_code="sys_admin"):
        yield signer


@pytest.fixture
async def other_applicant_client(db: AsyncSession) -> AsyncIterator[Signer]:
    """A fully registered applicant with nothing to do with this permit — the
    stranger whose own genuine certificate is crypto-valid and still not the
    holder's. Registered (its own `Applicant` row) because `get_current_user`
    gates an applicant-role user without one to a short exempt path list
    (`ERR-AUTH-008`), which does not include `/permits/*`."""
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    db.add(
        Applicant(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    )
    await db.flush()
    async for signer in _signer_for(db, role_code="applicant", user=user):
        yield signer


@pytest.fixture
async def non_signatory_client(db: AsyncSession) -> AsyncIterator[Signer]:
    """A staff role that is not one of the four signatories at all:
    `gis_specialist` holds no `permits.sign` grant (migration 0019), so the
    route's own dependency refuses it before the signatory check ever runs."""
    async for signer in _signer_for(db, role_code="gis_specialist"):
        yield signer


@pytest.fixture
async def holder_client(db: AsyncSession, applicant_user: User) -> AsyncIterator[Signer]:
    """The recipient: the individual applicant who OWNS `paid_application`.

    Deliberately not the re-exported `applicant_client` the brief named — that
    fixture is a fully registered applicant unrelated to any application here,
    and `test_issue.py` depends on it staying that way to prove the issuance
    permission gate. The 4th signature belongs to the permit's own holder, so it
    needs the account `make_paid_application` already created and linked through
    `applicants.owner_user_id`.
    """
    async for signer in _signer_for(db, role_code="applicant", user=applicant_user):
        yield signer


@pytest.fixture
async def issued_permit(
    db: AsyncSession, paid_application: Application, hodim_client: httpx.AsyncClient
) -> Permit:
    """A permit in `pending_signatures`, issued through the real route — never
    inserted by hand, so the document the signatures cover is the one issuance
    actually rendered and hashed (ruling 3)."""
    result = await hodim_client.post(f"/api/v1/applications/{paid_application.id}/permit")
    assert result.status_code == 201, result.text
    permit = await service.for_application(db, paid_application.id)
    assert permit is not None
    return permit


@pytest.fixture
async def permit_pdf(db: AsyncSession, issued_permit: Permit) -> bytes:
    """The stored bytes every signature is taken over — read back from storage,
    never re-rendered (ruling 3: there is only ever one document)."""
    return await service.pdf_bytes(db, issued_permit.id)


@pytest.fixture
async def override_required_signatures(db: AsyncSession):
    """Rewrite `permit_required_signatures` for one test, then take it back.

    `tests/modules/signatures/test_requirements.py::_override`'s idiom (merge the
    row the way `admin` does, then drop the 60-second per-process cache — the
    settings store is read-only by design and has getters only), plus the two
    things an HTTP test needs on top:

      * it COMMITS. The app runs on its own session and connection and cannot see
        a flush-only write; and every `_client_for`-style client commits `db`
        before each request anyway, so the row becomes permanent whether or not
        this fixture asks it to.
      * it DELETES the row afterwards. A missing row means "use the code
        default" (`settings_store`: the DB stores only overrides), so the delete
        is the restore — without it, `permit_required_signatures` would stay
        rewritten in this shared database and break every later test that reads
        it, in this run and the next.
    """
    key = "permit_required_signatures"

    async def _set(value: str) -> None:
        await db.merge(SystemSetting(key=key, value=value))
        await db.commit()
        invalidate(key)

    yield _set
    await db.execute(sa_delete(SystemSetting).where(SystemSetting.key == key))
    await db.commit()
    invalidate(key)


async def sign_permit(signer: Signer, permit_id: uuid.UUID, purpose: str, pdf: bytes):
    """One signature attempt over the stored document, with the signer's own ERI
    identity (`Signer`'s docstring explains why that identity is not a literal).

    Here rather than in `test_signatures.py`, where it started: Task 5's
    `active_permit` needs the same four calls to get a permit into force, and two
    copies of the envelope-building code are two things that can disagree about
    what a signature covers.
    """
    return await signer.client.post(
        f"/api/v1/permits/{permit_id}/signatures",
        json={
            "purpose": purpose,
            "pkcs7": encode_mock_signature(
                document=pdf, serial=signer.serial, issuer="ISS-1", pinfl=signer.pinfl
            ),
        },
    )


# --- Task 5: the anonymous public check --------------------------------------


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """A client with NO session cookie — the citizen or inspector С12 describes.

    Deliberately not `_client_for(db)`: that builds an `executor_staff` user and
    signs it in, which would prove nothing about a route whose whole contract is
    that it works without authentication. The commit hook is still needed —
    fixtures listed after this one in a test's parameter list are only flushed
    when the first request fires, and the app reads its own connection.
    """
    async with make_client(create_app(), lifespan=True) as anonymous:
        _commit_pending_before_requests(anonymous, db)
        yield anonymous


@pytest.fixture
async def active_permit(
    db: AsyncSession,
    issued_permit: Permit,
    permit_pdf: bytes,
    head_client: Signer,
    chief_forester_client: Signer,
    accountant_client: Signer,
    holder_client: Signer,
) -> Permit:
    """A permit in force, through the four real signatures.

    `active` is never assigned here by hand: `service._activate` is the ONE
    writer of that status and it runs only once `missing_purposes` comes back
    empty (C11), so a fixture that UPDATEd the column would give the public page
    a permit no signature ever made valid — and `signatures_valid` would then be
    asserted against a permit whose signatures do not exist.
    """
    for signer, purpose in (
        (head_client, "permit_head"),
        (chief_forester_client, "permit_chief_forester"),
        (accountant_client, "permit_accountant"),
        (holder_client, signers.RECIPIENT_PURPOSE),
    ):
        result = await sign_permit(signer, issued_permit.id, purpose, permit_pdf)
        assert result.status_code == 200, result.text
    # The app moved the row on its own session; `db` holds the object it loaded
    # before that and `expire_on_commit=False` never expires it (lesson).
    await db.refresh(issued_permit)
    assert issued_permit.status == "active"
    return issued_permit


# --- Task 6: the two provider seams ------------------------------------------


class _QueryCounter:
    """How many statements ran inside a `count_queries` block."""

    def __init__(self) -> None:
        self.value = 0


@asynccontextmanager
async def count_queries(db: AsyncSession) -> AsyncIterator[_QueryCounter]:
    """Count the SQL statements a block issues on `db`'s own connection.

    `AsyncSession.get_bind()` hands back the SYNC `Engine` underneath the async
    one — which is what `before_cursor_execute` is emitted on; there is no async
    flavour of the event. Nothing else in the suite counts queries, and this one
    exists for a single property: 3.6a reshaped `OCCUPANCY_PROVIDERS` from
    per-contour to batch so that registering a real provider could not turn a
    page of 20 contours into 20 round-trips, and only a count can hold that.

    Flush before entering the block, not inside it: SQLAlchemy's autoflush would
    otherwise charge a fixture's pending INSERTs to the code under test.
    """
    await db.flush()
    counter = _QueryCounter()
    engine = db.get_bind()

    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        counter.value += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _count)


async def make_permit_on_contour(
    db: AsyncSession,
    *,
    contour: Contour,
    version_id: uuid.UUID,
    org: Organization,
    activity_type_id: uuid.UUID,
    status: str,
    area_ha: Decimal = Decimal("12.5000"),
    sb_load: Decimal | None = Decimal("40.0000"),
    period_from: date = date(2027, 5, 1),
    period_to: date = date(2027, 9, 30),
) -> Permit:
    """A permit row on a GIVEN contour, in a GIVEN status, built through the ORM.

    Issuance cannot produce these: `service.issue` always creates its own contour
    through the application, `_activate` is the only writer of `active` and needs
    four real ERI signatures, and nothing in 3.11a writes `expired`, `suspended`
    or `revoked` at all — `suspended`/`revoked` are 3.11b's and `expired` is
    Task 7's own job, which must not be the fixture for the queries that read it.
    What the two providers assert is a SQL predicate over `status`, `contour_id`
    and the period, so the rows are built where the predicate can see them.

    Its own applicant and its own application every time: `permits.application_id`
    is unique and `ex_applications_no_duplicate` (migration 0015) forbids a second
    active-status application for the same (applicant, contour, activity) over an
    overlapping period — several permits on ONE contour is exactly what these
    tests need, so each gets a fresh applicant.

    The number comes from `permit_counters` through the real statement, never a
    literal: this database is shared and persistent, and a hard-coded number dies
    the first time issuance commits one (lesson).
    """
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    user.full_name = HOLDER_NAME
    applicant = Applicant(
        kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id
    )
    db.add(applicant)
    await db.flush()

    application = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=user.id,
        on_behalf="self",
        activity_type_id=activity_type_id,
        contour_id=contour.id,
        contour_version_id=version_id,
        requested_area_ha=area_ha,
        period_from=period_from,
        period_to=period_to,
        status="PERMIT_ISSUED",
        channel="portal",
        assigned_org_id=org.id,
    )
    db.add(application)
    await db.flush()

    series = get_settings().permit_series
    number = await repo.next_number(db, series)
    assert number is not None
    permit = Permit(
        series=series,
        number=number,
        application_id=application.id,
        applicant_id=applicant.id,
        activity_type_id=activity_type_id,
        organization_id=org.id,
        contour_id=contour.id,
        contour_version_id=version_id,
        area_ha=area_ha,
        period_from=period_from,
        period_to=period_to,
        amount=Decimal("2060000.00"),
        sb_load=sb_load,
        status=status,
        qr_token=secrets.token_urlsafe(32),
        snapshot={"holder_name": HOLDER_NAME},
    )
    db.add(permit)
    await db.flush()
    return permit


# --- 3.11b: the signed decision -----------------------------------------------


async def _reason_id(db: AsyncSession, code: str) -> uuid.UUID:
    """One `permit_status_reasons` item's id, by its PS-* code — migration
    0023's own seed, read the way `test_grounds.py::_item` already does."""
    classifier = await admin_repo.get_classifier_by_code(db, grounds.CLASSIFIER_CODE)
    assert classifier is not None, "migration 0023 must seed the classifier"
    items = await admin_repo.list_classifier_items(db, classifier.id)
    found = next((row for row in items if row.code == code), None)
    assert found is not None, f"migration 0023 must seed {code}"
    return found.id


@pytest.fixture
async def suspend_reason_id(db: AsyncSession) -> uuid.UUID:
    """PS-01 «Инспекция натижаси бўйича» — a ground that suspends and revokes."""
    return await _reason_id(db, "PS-01")


@pytest.fixture
async def resume_reason_id(db: AsyncSession) -> uuid.UUID:
    """PS-06 «Сабаб бартараф этилди» — the only ground that resumes."""
    return await _reason_id(db, "PS-06")


@pytest.fixture
async def revoke_reason_id(db: AsyncSession) -> uuid.UUID:
    return await _reason_id(db, "PS-03")


@pytest.fixture
async def hodim_user(db: AsyncSession, leshoz: Organization) -> User:
    """Whoever uploads the head's order — `files.save_upload` only needs an
    actor to record as `uploaded_by`; this task does not litigate who."""
    return await make_user(
        db, role_code="executor_staff", organization_id=leshoz.id, pinfl=unique_pinfl()
    )


@pytest.fixture
async def order_file_id(db: AsyncSession, hodim_user: User) -> uuid.UUID:
    """The head's order, as a real `media_files` row — the service checks that
    the file exists, is active and is a PDF before it takes the signature."""
    stored = await files.save_upload(
        db,
        data=b"%PDF-1.7\n% order\n",
        filename="buyruq.pdf",
        content_type="application/pdf",
        actor=hodim_user,
    )
    return stored.id


# suspend -> suspended, resume -> active, revoke -> revoked: the test-side half
# of `decisions.decide`'s own act/to_status split (`act` drives the ground and
# document rules, `to_status` drives the `tz/05` edge).
_TARGET: dict[str, str] = {
    grounds.SUSPEND: "suspended",
    grounds.RESUME: "active",
    grounds.REVOKE: "revoked",
}


async def sign_decision(
    signer: Signer,
    permit_id: uuid.UUID,
    act: str,
    *,
    reason_item_id: uuid.UUID,
    doc_file_id: uuid.UUID | None = None,
    legal_basis: str = "Лесхоз буйруғи №7",
) -> httpx.Response:
    """One decision attempt, signed over the canonical statement (ruling 3).

    The envelope is built over `decisions.decision_document(...)` — the same
    bytes the service will hand `sign()` — because `sign()` hashes what it is
    handed and never trusts the envelope's own claim of what it covered. A
    helper that signed anything else would test the mock rather than the route.
    """
    permit = await service.get(_db_of(signer), permit_id)
    assert permit is not None
    item = await admin_repo.get_classifier_item(_db_of(signer), reason_item_id)
    assert item is not None
    document = decisions.decision_document(
        permit=permit,
        to_status=_TARGET[act],
        reason_code=item.code,
        legal_basis=legal_basis,
        doc_file_id=doc_file_id,
    )
    return await signer.client.post(
        f"/api/v1/permits/{permit_id}/{act}",
        json={
            "reason_item_id": str(reason_item_id),
            "legal_basis": legal_basis,
            "doc_file_id": None if doc_file_id is None else str(doc_file_id),
            "pkcs7": encode_mock_signature(
                document=document, serial=signer.serial, issuer="ISS-1", pinfl=signer.pinfl
            ),
        },
    )


async def notification_rows(db: AsyncSession, *, object_id: uuid.UUID) -> list[Notification]:
    """Every notification written about one object, newest last."""
    rows = await db.execute(
        select(Notification)
        .where(Notification.object_id == object_id)
        .order_by(Notification.created_at, Notification.id)
    )
    return list(rows.scalars().all())
