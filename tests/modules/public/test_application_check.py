"""`GET /public/applications/check` — status without logging in (task 4).

Modeled on `tests/modules/public/test_appeals.py`'s check tests: the same
"no oracle" shape (a wrong `phone` and an unknown `number` answer
identically), proven the same way.

No `application_factory` fixture exists anywhere in the suite (checked
`tests/conftest.py` and `tests/modules/applications/conftest.py`) — the one
below builds an `applications` row directly through the ORM, the same idiom
`tests/modules/search/conftest.py::make_application` and `tests/modules/
oversight/conftest.py::make_bare_application` already use, rather than
driving a real submission through five other modules.

The task brief's own sketch guesses at names this file does not use, once
verified against the real code:

* `applications.models.APPLICATION_STATUSES` holds no `awaiting_payment` —
  fourteen upper-case members, `DRAFT` through `ARCHIVED`. `INVOICED` is the
  real status an unpaid, approved application sits in, so it stands in for
  the brief's invented one below.
* The applicant's contact phone lives on `auth.models.Applicant.phone`, not
  on `Application` itself — `Application.applicant_id` is the join.
* `Application.number` is the public number field the brief guessed right
  about, but its real prefix is `RX` (`applications.service.NUMBER_PREFIX`),
  not the brief's invented `AR-2026-004518`.
* No localized status-label dictionary existed anywhere in `applications`
  before this task; `public.service._APPLICATION_STATUS_INFO` is new, and
  the guard test below pins it against `APPLICATION_STATUSES` so the two can
  never drift apart.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import uuid7
from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.applications.models import APPLICATION_STATUSES, Application
from app.modules.auth.models import Applicant
from app.modules.public import service
from tests.conftest import make_client
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import leshoz as leshoz  # noqa: F401
from tests.modules.permits.conftest import grazing_activity_id as grazing_activity_id  # noqa: F401
from tests.modules.permits.conftest import unique_pinfl

API = "/api/v1"


@pytest.fixture
async def application_factory(
    db: AsyncSession, leshoz: Organization, grazing_activity_id: uuid.UUID
):
    """Returns an async `make(...)` building one `applications` row, its own
    fresh `Applicant`, directly through the ORM — never through a real
    submission, which would exercise `gis`/`norms`/`signatures` to prove a
    route that reads three plain columns.

    `leshoz.name` is patched to carry `uz_latn` once, here, before any row is
    built: the shared fixture (`tests/modules/gis/conftest.py`) predates
    decision #90's requirement and only sets `uz_cyrl`/`ru`, which would make
    `body["organization"]` compare against `None` in every test below rather
    than the leshoz's own name.
    """
    if "uz_latn" not in leshoz.name:
        leshoz.name = {**leshoz.name, "uz_latn": "Test Leshoz"}
        await db.flush()

    async def make(
        *,
        phone: str,
        status: str = "SUBMITTED",
        applicant_name: str = "Test Applicant",
        number: str | None = None,
    ) -> Application:
        applicant = Applicant(
            kind="individual", pinfl=unique_pinfl(), name=applicant_name, phone=phone
        )
        db.add(applicant)
        await db.flush()
        submitter = await make_user(db)
        application = Application(
            id=uuid7(),
            number=number or f"RX-TEST-{uuid.uuid4().hex[:8]}",
            applicant_id=applicant.id,
            submitted_by_user_id=submitter.id,
            on_behalf="self",
            activity_type_id=grazing_activity_id,
            assigned_org_id=leshoz.id,
            status=status,
            channel="portal",
            submitted_at=datetime(2026, 8, 28, tzinfo=UTC),
        )
        db.add(application)
        await db.flush()
        return application

    return make


async def test_a_wrong_pair_looks_exactly_like_no_match(
    db: AsyncSession, application_factory
) -> None:
    app = await application_factory(phone="+998901234567")
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        miss = await client.get(
            f"{API}/public/applications/check",
            params={"number": app.number, "phone": "+998900000000"},
        )
        unknown = await client.get(
            f"{API}/public/applications/check",
            params={"number": f"RX-UNKNOWN-{uuid.uuid4().hex[:8]}", "phone": "+998901234567"},
        )

    assert miss.status_code == unknown.status_code == 200
    assert miss.json() == unknown.json()
    assert miss.json()["found"] is False


async def test_a_matching_pair_returns_status_and_next_step(
    db: AsyncSession, application_factory
) -> None:
    app = await application_factory(phone="+998901234567", status="INVOICED")
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        r = await client.get(
            f"{API}/public/applications/check",
            params={"number": app.number, "phone": "+998901234567"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["number"] == app.number
    assert body["status"] == "INVOICED"
    assert body["status_label"]["uz_latn"]
    assert body["next_step"]
    assert body["activity_type"]
    assert body["organization"]
    assert body["submitted_at"] == "2026-08-28"


async def test_a_normalized_phone_still_matches(db: AsyncSession, application_factory) -> None:
    """The stored and given phone are compared on digits only (the same
    normalization `check_appeal_status` applies to its own contact) — a
    citizen who types spaces and dashes still finds their application."""
    app = await application_factory(phone="+998 90 123-45-67", status="SUBMITTED")
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        r = await client.get(
            f"{API}/public/applications/check",
            params={"number": app.number, "phone": "998901234567"},
        )

    assert r.json()["found"] is True


async def test_personal_data_never_appears(db: AsyncSession, application_factory) -> None:
    await application_factory(
        phone="+998901234567", applicant_name="Aliyev Vali", status="SUBMITTED"
    )
    app = await application_factory(
        phone="+998907654321", applicant_name="Aliyev Vali", status="SUBMITTED"
    )
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        r = await client.get(
            f"{API}/public/applications/check",
            params={"number": app.number, "phone": "+998907654321"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    flat = str(body)
    for forbidden in ("Aliyev", "Vali", "geometry", "coordinates", "amount"):
        assert forbidden not in flat
    # The whitelist itself (Global constraints): no key beyond what the
    # contract names may appear on the response at all.
    assert set(body) == {
        "found",
        "number",
        "status",
        "status_label",
        "activity_type",
        "organization",
        "next_step",
        "submitted_at",
    }


async def test_every_application_status_has_a_label_and_a_next_step() -> None:
    """`public.service._APPLICATION_STATUS_INFO` maps `APPLICATION_STATUSES`,
    not a copy of it — this guard is what keeps the two from drifting apart
    the way `test_public_surface.py::test_transition_table_has_all_fourteen_
    statuses_and_only_real_targets` already does for `APPLICATION_TRANSITIONS`.
    """
    assert set(service._APPLICATION_STATUS_INFO) == set(APPLICATION_STATUSES)
    for status, info in service._APPLICATION_STATUS_INFO.items():
        assert info["label"]["uz_latn"], status
        assert info["next_step"]["uz_latn"], status
