"""The three read routes: the list, the card and the stored PDF.

**Every one of them needs BOTH checks** (lesson: zone scoping is not a
permission check). `permits.view_any` answers "may this role see permits beyond
its own at all"; the zone answers "whose". A holder passes neither and still
reads their own permit, which is why the rule lives in the service and not in a
route-level `require_permission` — the same shape
`signatures.service.list_signatures_page` uses for exactly the same reason.

Assertions are scoped by ids this test created — `applicant_id`, `contour_id` —
never by `organization_id` or by page membership: `leshoz` is shared across
runs and this database is persistent, so a permit issued by any earlier run of
any test file is sitting in that organization's list (lesson).
"""

import uuid
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.schemas import PageParams
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.audit.models import AuditLog
from app.modules.permits import service
from app.modules.permits.models import Permit
from app.modules.permits.permissions import PERMITS_VIEW_ANY
from tests.modules.gis.conftest import _client_for
from tests.modules.permits.conftest import Signer

API = "/api/v1"


@pytest.fixture
async def zone_staff_client(db: AsyncSession, leshoz: Organization):
    """An `executor_staff` zoned to the very leshoz the fixture permits belong
    to. `permits.view_any` is listed although migration 0019 grants it to the
    role anyway — the fixture states the grant the production role holds rather
    than relying on inheritance being remembered (lesson)."""
    async for client in _client_for(db, PERMITS_VIEW_ANY, organization_id=leshoz.id):
        yield client


# --- GET /permits/{id} — the card --------------------------------------------


async def test_the_holder_reads_their_own_permit_card(holder_client: Signer, issued_permit: Permit):
    result = await holder_client.client.get(f"{API}/permits/{issued_permit.id}")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["id"] == str(issued_permit.id)
    assert body["series"] == issued_permit.series
    assert body["number"] == issued_permit.number
    assert body["status"] == "pending_signatures"
    # Not yet signed by anybody, and every required purpose still missing.
    assert body["signatures"] == []
    assert body["missing_signatures"]
    assert [(row["from_status"], row["to_status"]) for row in body["history"]] == [
        (None, "pending_signatures")
    ]


async def test_the_card_carries_the_signatures_and_the_timeline(
    active_permit: Permit, holder_client: Signer
):
    result = await holder_client.client.get(f"{API}/permits/{active_permit.id}")
    assert result.status_code == 200, result.text
    body = result.json()
    assert {row["purpose"] for row in body["signatures"]} == {
        "permit_head",
        "permit_chief_forester",
        "permit_accountant",
        "permit_recipient",
    }
    assert all(row["verification_status"] == "valid" for row in body["signatures"])
    # The envelope itself is never on the card: it is large, and 3.8's own
    # `GET /signatures?object_type=permit&object_id=` answers the full row.
    assert "signature_value" not in body["signatures"][0]
    assert body["missing_signatures"] == []


async def test_staff_in_zone_read_a_permit_they_do_not_hold(
    zone_staff_client: httpx.AsyncClient, issued_permit: Permit
):
    result = await zone_staff_client.get(f"{API}/permits/{issued_permit.id}")
    assert result.status_code == 200, result.text


async def test_a_stranger_cannot_read_someone_elses_permit(
    other_applicant_client: Signer, active_permit: Permit
):
    """404, not 403: a permit's very existence is information, and the answer to
    "is there a permit with this id" must not differ between a real one somebody
    else holds and one that never existed (the same oracle reasoning ruling 8
    applies to the anonymous check)."""
    result = await other_applicant_client.client.get(f"{API}/permits/{active_permit.id}")
    assert result.status_code == 404
    assert result.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_staff_role_without_view_any_is_refused_like_a_stranger(
    non_signatory_client: Signer, active_permit: Permit
):
    """`gis_specialist` holds no `permits.view_any` (migration 0019), and being
    staff is not by itself a reason to see a citizen's permit."""
    result = await non_signatory_client.client.get(f"{API}/permits/{active_permit.id}")
    assert result.status_code == 404


async def test_staff_outside_the_zone_are_refused_with_the_territorial_code(
    other_zone_hodim_client: httpx.AsyncClient, active_permit: Permit
):
    """403 `ERR-ACL-002`, not 404: this actor holds `permits.view_any`, so the
    refusal is territorial and says so — the same answer `_assert_in_zone` gives
    on the issuance path, and `tz/10`'s RI-12 is about exactly this attempt."""
    result = await other_zone_hodim_client.get(f"{API}/permits/{active_permit.id}")
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "ERR-ACL-002"


async def test_an_unknown_permit_id_is_a_404(zone_staff_client: httpx.AsyncClient):
    result = await zone_staff_client.get(f"{API}/permits/{uuid.uuid4()}")
    assert result.status_code == 404


async def test_the_card_needs_a_session(client: httpx.AsyncClient, active_permit: Permit):
    """The only permit route the open internet reaches is
    `GET /public/permits/check`."""
    assert (await client.get(f"{API}/permits/{active_permit.id}")).status_code == 401


# --- GET /permits/{id}/pdf ---------------------------------------------------


async def test_the_holder_downloads_the_stored_document(
    holder_client: Signer, issued_permit: Permit, permit_pdf: bytes
):
    result = await holder_client.client.get(f"{API}/permits/{issued_permit.id}/pdf")
    assert result.status_code == 200, result.text
    # The STORED bytes, byte for byte — never a re-render (ruling 3).
    assert result.content == permit_pdf
    assert result.headers["content-type"].startswith("application/pdf")
    assert result.headers["x-content-type-options"] == "nosniff"
    # RFC 6266/5987: the series is Cyrillic «А», which is not latin-1 encodable,
    # so the raw name may travel only in `filename*` — a raw byte in the legacy
    # `filename=` half makes Starlette raise on every download (lesson).
    disposition = result.headers["content-disposition"]
    ascii_half, _, extended = disposition.partition("filename*=UTF-8''")
    assert ascii_half.startswith("inline;")
    assert issued_permit.series not in ascii_half
    assert extended == quote(
        f"permit-{issued_permit.series}-{issued_permit.number:06d}.pdf", safe=""
    )


async def test_a_stranger_cannot_download_someone_elses_pdf(
    other_applicant_client: Signer, active_permit: Permit
):
    result = await other_applicant_client.client.get(f"{API}/permits/{active_permit.id}/pdf")
    assert result.status_code == 404


async def test_staff_outside_the_zone_cannot_download_the_pdf(
    other_zone_hodim_client: httpx.AsyncClient, active_permit: Permit
):
    result = await other_zone_hodim_client.get(f"{API}/permits/{active_permit.id}/pdf")
    assert result.status_code == 403


# --- the RI-12 trail on a territorial read denial (ruling T8-a) --------------


async def _ri12_entries(db: AsyncSession, permit: Permit) -> list[AuditLog]:
    """Every denied-read entry against this permit. Scoped by the permit's own id
    because the database is shared and persistent (lesson)."""
    rows = await db.scalars(
        select(AuditLog)
        .where(
            AuditLog.object_id == permit.id,
            AuditLog.action == service.PERMIT_READ,
        )
        .order_by(AuditLog.occurred_at)
    )
    return list(rows.all())


async def test_a_cross_zone_card_read_writes_the_ri12_trail(
    db: AsyncSession, other_zone_hodim_client: httpx.AsyncClient, active_permit: Permit
):
    """`tz/10` RI-12 is «попытка доступа вне территориальных полномочий», High and
    immediate. This module already records the same actor's attempt to SIGN a
    permit outside their zone; without this, stage 4.2's risk report would see
    who tried to sign one and miss who tried to look at one.

    The trail must SURVIVE the refusal — decision #40's early-commit-on-denial —
    so this asserts it after a 403 that raised, not after a success."""
    assert await _ri12_entries(db, active_permit) == []

    result = await other_zone_hodim_client.get(f"{API}/permits/{active_permit.id}")
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "ERR-ACL-002"

    entries = await _ri12_entries(db, active_permit)
    assert len(entries) == 1
    assert entries[0].result == "denied"
    assert entries[0].basis == "out_of_zone"
    assert entries[0].extra == {"risk_indicator": "RI-12"}
    assert entries[0].user_id is not None
    assert entries[0].object_type == "permit"


async def test_a_cross_zone_pdf_read_writes_it_too(
    db: AsyncSession, other_zone_hodim_client: httpx.AsyncClient, active_permit: Permit
):
    """Both direct-access routes reach `_readable_permit`, so both are covered by
    the one branch — asserted here rather than assumed, since a route that
    fetched the permit itself would silently skip it."""
    assert (
        await other_zone_hodim_client.get(f"{API}/permits/{active_permit.id}/pdf")
    ).status_code == 403
    assert len(await _ri12_entries(db, active_permit)) == 1


async def test_a_successful_read_writes_no_trail(
    db: AsyncSession, zone_staff_client: httpx.AsyncClient, active_permit: Permit
):
    """A row per GET would let anyone holding a session write `audit_log` at
    will. `permit.read` exists for the denial and for nothing else."""
    assert (await zone_staff_client.get(f"{API}/permits/{active_permit.id}")).status_code == 200
    assert await _ri12_entries(db, active_permit) == []


async def test_a_stranger_s_404_writes_no_trail(
    db: AsyncSession, other_applicant_client: Signer, active_permit: Permit
):
    """A caller holding no `permits.view_any` cannot be "outside their zone" —
    there is no zone claim to exceed — and auditing it would let any signed-in
    citizen fill the table by guessing uuids."""
    assert (
        await other_applicant_client.client.get(f"{API}/permits/{active_permit.id}")
    ).status_code == 404
    assert await _ri12_entries(db, active_permit) == []


async def test_the_list_cannot_produce_a_territorial_denial(
    db: AsyncSession, other_zone_hodim_client: httpx.AsyncClient, active_permit: Permit
):
    """RI-12 coverage on reads is DIRECT ACCESS ONLY, and this is the test that
    says so out loud for 4.2: nobody named a target, so `zone_filter` returns
    fewer rows and there is no attempt to record. A probe that walks the list is
    the rate limiter's problem, not the audit trail's."""
    result = await other_zone_hodim_client.get(
        f"{API}/permits", params={"contour_id": str(active_permit.contour_id)}
    )
    assert result.status_code == 200
    assert result.json()["total"] == 0
    assert await _ri12_entries(db, active_permit) == []


# --- GET /permits — the list -------------------------------------------------


async def test_the_holder_sees_their_own_permits_and_only_those(
    db: AsyncSession, holder_client: Signer, issued_permit: Permit
):
    """The holder's applicant row is created fresh per test, so an exact list is
    a safe assertion here — unlike anything scoped by `leshoz`."""
    result = await holder_client.client.get(f"{API}/permits")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["total"] == 1
    assert [row["id"] for row in body["items"]] == [str(issued_permit.id)]
    assert body["page"] == 1
    assert body["page_size"] == 20


async def test_a_stranger_sees_an_empty_list_rather_than_someone_elses_permits(
    other_applicant_client: Signer, active_permit: Permit
):
    result = await other_applicant_client.client.get(f"{API}/permits")
    assert result.status_code == 200, result.text
    assert result.json()["total"] == 0
    assert result.json()["items"] == []


async def test_staff_in_zone_see_a_permit_they_do_not_hold(
    zone_staff_client: httpx.AsyncClient, issued_permit: Permit
):
    result = await zone_staff_client.get(
        f"{API}/permits", params={"contour_id": str(issued_permit.contour_id)}
    )
    assert result.status_code == 200, result.text
    assert [row["id"] for row in result.json()["items"]] == [str(issued_permit.id)]


async def test_staff_outside_the_zone_see_nothing_of_it(
    other_zone_hodim_client: httpx.AsyncClient, issued_permit: Permit
):
    """A filter cannot answer 403 — it answers with fewer rows. That is the
    whole point of `zone_filter` and the reason the card's own refusal is
    territorial while this one is simply empty."""
    result = await other_zone_hodim_client.get(
        f"{API}/permits", params={"contour_id": str(issued_permit.contour_id)}
    )
    assert result.status_code == 200, result.text
    assert result.json()["total"] == 0


async def test_every_filter_narrows_the_list(
    zone_staff_client: httpx.AsyncClient, issued_permit: Permit
):
    """Each filter, once against a value that matches and once against one that
    does not — a filter silently ignored would pass a matching-value-only
    test."""
    base = {"contour_id": str(issued_permit.contour_id)}
    for hit, miss in (
        ({"status": "pending_signatures"}, {"status": "active"}),
        ({"applicant_id": str(issued_permit.applicant_id)}, {"applicant_id": str(uuid.uuid4())}),
        (
            {"organization_id": str(issued_permit.organization_id)},
            {"organization_id": str(uuid.uuid4())},
        ),
        (
            {"series": issued_permit.series, "number": issued_permit.number},
            {"series": issued_permit.series, "number": issued_permit.number + 10**6},
        ),
    ):
        found = await zone_staff_client.get(f"{API}/permits", params={**base, **hit})
        assert found.status_code == 200, found.text
        assert [row["id"] for row in found.json()["items"]] == [str(issued_permit.id)], hit
        empty = await zone_staff_client.get(f"{API}/permits", params={**base, **miss})
        assert empty.status_code == 200, empty.text
        assert empty.json()["total"] == 0, miss


async def test_a_contour_filter_alone_is_scoped_to_this_permit(
    zone_staff_client: httpx.AsyncClient, issued_permit: Permit
):
    result = await zone_staff_client.get(f"{API}/permits", params={"contour_id": str(uuid.uuid4())})
    assert result.json()["total"] == 0


async def test_an_unbounded_number_is_a_422_and_never_a_500(
    zone_staff_client: httpx.AsyncClient,
):
    """`permits.number` is a bigint; an integer past that range reaches asyncpg
    as `DataError: value out of int64 range` (lesson), so the bound belongs on
    the query parameter."""
    result = await zone_staff_client.get(f"{API}/permits", params={"number": str(2**63)})
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "ERR-VAL-001"


async def test_an_unknown_status_is_refused_rather_than_matching_nothing(
    zone_staff_client: httpx.AsyncClient,
):
    """A typo'd status must not read as "no permits in that state"."""
    result = await zone_staff_client.get(f"{API}/permits", params={"status": "pendign"})
    assert result.status_code == 422


async def test_the_list_needs_a_session(client: httpx.AsyncClient):
    assert (await client.get(f"{API}/permits")).status_code == 401


# --- the service call the routes are built on --------------------------------


async def test_permit_card_and_list_agree_on_who_the_holder_is(
    db: AsyncSession,
    issued_permit: Permit,
    holder_client: Signer,
    paid_application: Application,
):
    """One rule, asked two ways: the card admits the holder and the list
    contains their permit. Two separate definitions of "holder" is how a permit
    becomes readable by id and invisible in the list that should carry it."""
    card = await service.permit_card(db, issued_permit.id, actor=holder_client.user)
    assert card["permit"].id == issued_permit.id

    items, total = await service.list_permits(db, actor=holder_client.user, params=PageParams())
    assert total == 1
    assert [row.id for row in items] == [issued_permit.id]
    assert issued_permit.application_id == paid_application.id
