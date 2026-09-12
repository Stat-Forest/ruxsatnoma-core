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

from app.core.models import MediaFile
from app.core.schemas import PageParams
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.audit.models import AuditLog
from app.modules.gis.models import GisLayer
from app.modules.permits import service
from app.modules.permits.models import Permit
from app.modules.permits.permissions import PERMITS_VIEW_ANY
from tests.modules.gis.conftest import _client_for, make_contour, make_version, random_box_wkt
from tests.modules.permits.conftest import Signer, make_permit_on_contour

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
    # `document_date` is the frozen `snapshot["issued_at"]` (`service._snapshot`'s
    # requisite 3, "Берилган сана", Tashkent-local) — never `issued_at]` (the
    # activation timestamp, UTC), the demo-sprint defect (`docs/status.md`).
    assert body["document_date"] == active_permit.snapshot["issued_at"]


async def test_document_date_is_the_frozen_document_date_not_the_activation_day(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    paid_application: Application,
    hodim_client: httpx.AsyncClient,
    head_client: Signer,
    chief_forester_client: Signer,
    accountant_client: Signer,
    holder_client: Signer,
) -> None:
    """The demo-sprint defect, reproduced: `docs/status.md` "`Berilgan sana`
    renders in UTC while the signatures beside it render in local time, so a
    permit issued late in the evening shows yesterday's date." `issued_at` (the
    card's other date-like field) is stamped by `_activate` on the day the LAST
    signature lands, which can be a different calendar day than the one
    `_snapshot` froze into the document at ISSUANCE (`business_today()`,
    Tashkent) — a citizen may take days to gather four signatures. Frozen five
    days apart here (issuance patched well into the past, activation left
    real) — a one-day gap would be indistinguishable from the UTC/Tashkent
    offset itself near midnight, exactly the ambiguity this fix removes, so
    the test needs a gap no clock straddling can produce by coincidence — to
    prove `document_date` reads the DOCUMENT's own day and never drifts to the
    activation day, unlike `issued_at`."""
    from datetime import timedelta

    from app.core.time import business_today
    from app.modules.permits import service
    from tests.modules.permits.conftest import sign_permit

    issuance_day = business_today() - timedelta(days=5)
    monkeypatch.setattr(service, "business_today", lambda: issuance_day)
    result = await hodim_client.post(f"/api/v1/applications/{paid_application.id}/permit")
    assert result.status_code == 201, result.text
    monkeypatch.undo()  # activation below must stamp the REAL day, not issuance_day

    permit = await service.for_application(db, paid_application.id)
    assert permit is not None
    pdf = await service.pdf_bytes(db, permit.id)
    for signer, purpose in (
        (head_client, "permit_head"),
        (chief_forester_client, "permit_chief_forester"),
        (accountant_client, "permit_accountant"),
        (holder_client, "permit_recipient"),
    ):
        signed = await sign_permit(signer, permit.id, purpose, pdf)
        assert signed.status_code == 200, signed.text

    card = await holder_client.client.get(f"{API}/permits/{permit.id}")
    body = card.json()
    assert body["document_date"] == issuance_day.isoformat()
    assert body["document_date"] != body["issued_at"][:10]


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


# --- the read-side half of ruling 4: the three official signers -------------
#
# The defect this section pins: `executor_head`, `chief_forester` and
# `accountant` hold `permits.sign` and are exactly who `add_signature` lets
# attach a signature, but `_readable_permit` admitted only the holder and a
# `permits.view_any` holder — so all three got the same 404 as a stranger and
# could never open the permit they are required to sign through any UI, only
# through a direct API call to the write route itself.


async def test_the_head_reads_a_permit_they_are_required_to_sign(
    head_client: Signer, issued_permit: Permit
):
    result = await head_client.client.get(f"{API}/permits/{issued_permit.id}")
    assert result.status_code == 200, result.text
    assert result.json()["id"] == str(issued_permit.id)


async def test_the_chief_forester_reads_a_permit_they_are_required_to_sign(
    chief_forester_client: Signer, issued_permit: Permit
):
    result = await chief_forester_client.client.get(f"{API}/permits/{issued_permit.id}")
    assert result.status_code == 200, result.text
    assert result.json()["id"] == str(issued_permit.id)


async def test_the_accountant_reads_a_permit_they_are_required_to_sign(
    accountant_client: Signer, issued_permit: Permit
):
    result = await accountant_client.client.get(f"{API}/permits/{issued_permit.id}")
    assert result.status_code == 200, result.text
    assert result.json()["id"] == str(issued_permit.id)


async def test_a_same_role_signer_of_a_different_organization_still_cannot_read_it(
    other_org_head_client: Signer, issued_permit: Permit
):
    """Same role, same `permits.sign` grant, wrong leshoz — `_is_required_signer`
    checks the SAME strict `users.organization_id == permit.organization_id`
    equality `_signer_refusal` uses on the write path (lesson: zone scoping is
    not a permission check). `executor_head` holds no `permits.view_any`
    either (migration 0019), so this falls through to the same 404 a stranger
    gets, exactly like `test_a_staff_role_without_view_any_is_refused_like_a_stranger`."""
    result = await other_org_head_client.client.get(f"{API}/permits/{issued_permit.id}")
    assert result.status_code == 404
    assert result.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_required_signer_still_reads_the_permit_once_it_is_active(
    head_client: Signer, active_permit: Permit
):
    """Access does not expire at signing or at ACTIVE (`_is_required_signer`'s
    own docstring): the head who signed `active_permit` can still open it
    afterwards, the same way the holder never loses access to their own."""
    result = await head_client.client.get(f"{API}/permits/{active_permit.id}")
    assert result.status_code == 200, result.text


async def test_a_required_signer_downloads_the_pdf_too(
    accountant_client: Signer, issued_permit: Permit, permit_pdf: bytes
):
    """Both direct-access routes reach `_readable_permit` — pinned for the
    zone branch by `test_a_cross_zone_pdf_read_writes_it_too`, and the same
    must hold for the new admission."""
    result = await accountant_client.client.get(f"{API}/permits/{issued_permit.id}/pdf")
    assert result.status_code == 200, result.text
    assert result.content == permit_pdf


async def test_a_required_signers_read_writes_no_ri12_trail(
    db: AsyncSession, chief_forester_client: Signer, issued_permit: Permit
):
    """Not the territorial branch: a same-organization required signer is not
    "outside their zone" any more than the holder is, so an ordinary read
    leaves no RI-12 row (mirrors `test_a_successful_read_writes_no_trail`)."""
    result = await chief_forester_client.client.get(f"{API}/permits/{issued_permit.id}")
    assert result.status_code == 200, result.text
    assert await _ri12_entries(db, issued_permit) == []


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


# --- the list half of ruling 4's read side ------------------------------------
#
# The defect these pin, found by the stage 7.3 walkthrough: the card was fixed
# for the three official signers (`_is_required_signer`, above) and the LIST was
# not, so `list_permits`' own docstring — "the scope is the UNION of the two
# things `_readable_permit` admits one at a time, so the list can never disagree
# with the card" — became untrue the moment `_readable_permit` began admitting a
# third. On dev a leshoz head whose leshoz held three permits got `200` with an
# empty list, no menu entry, and a card that answered "no permit exists" for the
# by-application URL the UI uses. A signer who cannot FIND the permit cannot
# reach the sign screen, which is the same defect the card fix closed, one step
# earlier.


async def test_the_head_finds_in_the_list_the_permit_they_must_sign(
    head_client: Signer, issued_permit: Permit
):
    result = await head_client.client.get(
        f"{API}/permits", params={"contour_id": str(issued_permit.contour_id)}
    )
    assert result.status_code == 200, result.text
    assert [row["id"] for row in result.json()["items"]] == [str(issued_permit.id)]


async def test_the_chief_forester_finds_in_the_list_the_permit_they_must_sign(
    chief_forester_client: Signer, issued_permit: Permit
):
    result = await chief_forester_client.client.get(
        f"{API}/permits", params={"contour_id": str(issued_permit.contour_id)}
    )
    assert result.status_code == 200, result.text
    assert [row["id"] for row in result.json()["items"]] == [str(issued_permit.id)]


async def test_the_accountant_finds_in_the_list_the_permit_they_must_sign(
    accountant_client: Signer, issued_permit: Permit
):
    result = await accountant_client.client.get(
        f"{API}/permits", params={"contour_id": str(issued_permit.contour_id)}
    )
    assert result.status_code == 200, result.text
    assert [row["id"] for row in result.json()["items"]] == [str(issued_permit.id)]


async def test_a_same_role_signer_of_another_organization_still_sees_nothing(
    other_org_head_client: Signer, issued_permit: Permit
):
    """The list mirrors the card's own strict organization equality, never the
    three-axis zone predicate: there is no republic-wide leshoz head, and a head
    of a different leshoz gets an empty list here exactly as they get a 404 on
    the card."""
    result = await other_org_head_client.client.get(
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


async def test_the_list_puts_the_most_recently_updated_permit_first(
    db: AsyncSession,
    zone_staff_client: httpx.AsyncClient,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    grazing_activity_id: uuid.UUID,
):
    """`updated_at DESC`, `id DESC` only as the tie-break: an older permit that
    was just moved (suspended here, through the ORM row exactly as
    `service.set_status` writes it) climbs above a newer untouched one. Two
    permits on ONE fresh contour, so the `contour_id` filter makes the list
    exact — the zone alone would not (`leshoz` shares a district across tests).

    The commit between creation and the touch matters: Postgres `now()` is the
    transaction's start, so an UPDATE in the INSERTs' own transaction would
    stamp the same `updated_at` the rows were born with and prove nothing."""
    contour = await make_contour(db, contours_layer, leshoz)
    version = await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    older, newer = [
        await make_permit_on_contour(
            db,
            contour=contour,
            version_id=version.id,
            org=leshoz,
            activity_type_id=grazing_activity_id,
            status="active",
        )
        for _ in range(2)
    ]
    await db.commit()

    older.status = "suspended"
    await db.commit()

    listed = await zone_staff_client.get(f"{API}/permits", params={"contour_id": str(contour.id)})
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["items"]] == [str(older.id), str(newer.id)]


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


async def test_q_finds_a_permit_by_the_holders_name_and_by_its_printed_number(
    zone_staff_client: httpx.AsyncClient, issued_permit: Permit
):
    """`q` is the one free-text filter the register has (the former `/search`
    screen folded into it): a substring of the holder's name, case-insensitive,
    or of the printed `"<series> № <000000>"` — and nothing for a stranger's
    name."""
    by_name = await zone_staff_client.get(f"{API}/permits", params={"q": "азизов"})
    assert by_name.status_code == 200, by_name.text
    assert [row["id"] for row in by_name.json()["items"]] == [str(issued_permit.id)]

    printed = f"{issued_permit.series} № {issued_permit.number:06d}"
    by_number = await zone_staff_client.get(f"{API}/permits", params={"q": printed})
    assert [row["id"] for row in by_number.json()["items"]] == [str(issued_permit.id)]

    nobody = await zone_staff_client.get(f"{API}/permits", params={"q": "Каримов"})
    assert nobody.json()["total"] == 0
