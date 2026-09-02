"""С12 — the anonymous QR page, the other end of the symbol printed on every
permit.

Three properties this file exists to pin, each a security property rather than a
feature:

  * **a miss is 200, never 404.** An anonymous endpoint answering 404 for an
    unknown number and 200 for a known one is a free permit-number oracle: one
    shape for both, `{"found": false}` (ruling 8).
  * **the card is masked, and it is read off the frozen snapshot**, never off the
    live applicant row — the page must agree with the paper it is printed on.
  * **the rate limit is the control.** `?series=&number=` is guessable by
    construction, so the token's secrecy protects the QR path alone. CAPTCHA is
    stage 6's.

`test_every_check_is_counted_without_personal_data` deviates from the brief's
verbatim body: it counts only the rows it created ITSELF. `qr_check_log` is
append-only statistics in a shared, persistent test database, so an unscoped
`count(*) == 2` passes exactly once — and never again once the rate-limit test
below has written sixty rows of its own (lesson: the test DB is never empty,
including the spot you picked). The scope is a `uuid7` marker taken before the
first request: ids are time-ordered and the app generates them in this very
process, so `id > marker` is exact and owes nothing to the database's clock.
"""

import logging
import uuid
from typing import Any, get_args

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ratelimit
from app.core.logging import _SilentPathFilter, configure_logging
from app.db import uuid7
from app.main import create_app
from app.modules.auth.models import Applicant
from app.modules.permits import service
from app.modules.permits.models import (
    PERMIT_STATUSES,
    QR_CHECK_CHANNELS,
    QR_CHECK_RESULTS,
    Permit,
    QrCheckLog,
)
from app.modules.permits.schemas import PublicStatus
from app.modules.signatures import service as signatures_service
from app.modules.signatures.models import Signature

CHECK = "/api/v1/public/permits/check"

# Every key a found card carries — and, by being an equality, every key it must
# NOT: `qr_token` is the key to this very page and `holder_pinfl` is requisite
# 10's identity half, both of which the snapshot holds and neither of which may
# leave the building unauthenticated.
CARD_FIELDS = {
    "found",
    "status",
    "valid_from",
    "valid_to",
    "organization",
    "activity_type",
    "signatures_valid",
    "holder",
}


async def _rows_since(db: AsyncSession, marker: uuid.UUID) -> list[QrCheckLog]:
    """The `qr_check_log` rows written after `marker`, oldest first."""
    return list(
        (
            await db.scalars(
                select(QrCheckLog).where(QrCheckLog.id > marker).order_by(QrCheckLog.id)
            )
        ).all()
    )


# --- the pure halves: the mask and the two maps -------------------------------


def test_the_mask_keeps_an_initial_and_an_ending_and_hides_the_length():
    """С12: «Персональные данные — маскированы (ФИО сокращённо)». The mask is a
    fixed three asterisks whatever it covers — a variable-width one would leak
    how long the surname is, which is most of a surname."""
    assert service.mask_name("Азизов Азиз Азизович") == "А.***ов А."
    # Twice as long, masked to the same width.
    assert service.mask_name("Абдурахмонов Азиз") == "А.***ов А."
    # A surname alone is still a card, not a crash.
    assert service.mask_name("Азизов") == "А.***ов"
    assert service.mask_name("  Азизов   Азиз  ") == "А.***ов А."
    # A hyphen falls inside the masked middle like any other character.
    assert service.mask_name("Абдулла-Зода Азиз") == "А.***да А."
    # Too short to keep an ending: "Ли" shown as "Л.***Ли" would be no mask at
    # all, and a four-letter name would hide exactly one character.
    assert service.mask_name("Ли Азиз") == "Л.*** А."
    assert service.mask_name("Азиз Азиз") == "А.*** А."
    assert service.mask_name("А") == "А.***"
    # Never an empty string and never an exception — this runs unauthenticated.
    assert service.mask_name("   ") == service.NOT_STATED


def test_every_permit_status_has_a_decided_public_answer():
    """The map is what 3.11b (`suspended`/`revoked`) and 4.7 (`archived`) will
    rely on, so it is complete now: every status is either one of С12's four
    words or explicitly not public. A status in neither set is a status whose
    public answer nobody decided, which is exactly the fall-through this
    equality exists to make impossible."""
    assert set(service.PUBLIC_STATUS_LABELS) | set(service.NON_PUBLIC_STATUSES) == set(
        PERMIT_STATUSES
    )
    assert not set(service.PUBLIC_STATUS_LABELS) & set(service.NON_PUBLIC_STATUSES)
    # The schema's Literal is spelled out by hand (pyright needs the members
    # statically); this is the assertion that closes the gap (lesson).
    assert set(get_args(PublicStatus)) == set(service.PUBLIC_STATUS_LABELS.values())
    # Same idiom for the log's two enum-ish columns, whose CHECKs are built from
    # the tuples in `models.py`.
    assert {service.CHANNEL_QR, service.CHANNEL_MANUAL} == set(QR_CHECK_CHANNELS)
    assert {service.RESULT_FOUND, service.RESULT_NOT_FOUND} == set(QR_CHECK_RESULTS)


def test_the_log_has_no_column_that_could_identify_a_visitor():
    """`design/02`: «No IP addresses and no personal data». The route is
    anonymous, so the log is statistics and not a trail — and the guarantee is
    the table's own shape rather than the care of whoever writes to it."""
    assert {column.name for column in QrCheckLog.__table__.columns} == {
        "id",
        "permit_id",
        "occurred_at",
        "result",
        "channel",
    }


# --- the brief's six ----------------------------------------------------------


async def test_the_qr_token_returns_the_masked_card(
    client: httpx.AsyncClient, active_permit: Permit
):
    result = await client.get(CHECK, params={"qr": active_permit.qr_token})
    assert result.status_code == 200

    body = result.json()
    assert body["found"] is True
    assert body["status"] == "амалда"
    assert body["signatures_valid"] is True
    assert body["organization"]
    assert body["activity_type"]
    assert body["holder"] == "А.***ов А.", "С12: personal data is masked"
    assert "pinfl" not in body and "qr_token" not in body


async def test_series_and_number_work_for_a_citizen_holding_paper(
    client: httpx.AsyncClient, active_permit: Permit
):
    result = await client.get(
        CHECK, params={"series": active_permit.series, "number": active_permit.number}
    )
    assert result.status_code == 200
    assert result.json()["found"] is True


async def test_an_unknown_permit_answers_200_not_404(client: httpx.AsyncClient):
    """An anonymous 404-vs-200 difference is a permit-number oracle."""
    result = await client.get(CHECK, params={"qr": "nope"})
    assert result.status_code == 200
    assert result.json() == {"found": False}


async def test_a_permit_still_awaiting_signatures_is_not_public(
    client: httpx.AsyncClient, issued_permit: Permit
):
    """The QR page goes live when the permit does — design/03. A document that
    is not yet ACTIVE must not read as one."""
    result = await client.get(CHECK, params={"qr": issued_permit.qr_token})
    assert result.json() == {"found": False}


async def test_every_check_is_counted_without_personal_data(
    db: AsyncSession, client: httpx.AsyncClient, active_permit: Permit
):
    marker = uuid7()
    await client.get(CHECK, params={"qr": active_permit.qr_token})
    await client.get(CHECK, params={"qr": "nope"})

    rows = await _rows_since(db, marker)
    assert {row.result for row in rows} == {"found", "not_found"}
    assert all(not hasattr(row, "ip") for row in rows)
    assert (
        await db.scalar(select(func.count()).select_from(QrCheckLog).where(QrCheckLog.id > marker))
        == 2
    )


def test_the_printed_qr_encodes_the_page_a_human_reads_not_the_json_route() -> None:
    """Ruling F-1. Requisite 24's whole purpose is that a citizen scanning a printed
    permit lands on something readable; `{public_base_url}/api/v1/public/permits/check`
    would hand them the raw JSON this file's other tests assert on. The route stays
    exactly where `design/03` puts it (ruling 15 is about the ROUTE, not about what the
    QR ENCODES) — the QR points at the front-end page that CALLS it.

    Pinned rather than left to review, because `qr_url`'s own docstring says it: a
    permit is printed once and the URL on it cannot be corrected afterwards. Stage 6
    must serve `/check` before the first production permit is issued (`README.md`)."""
    url = service.qr_url("tok-123")

    assert url.endswith("/check?qr=tok-123"), url
    assert "/api/" not in url, url


async def test_the_route_is_rate_limited(client: httpx.AsyncClient, active_permit: Permit):
    """Ruling 8: the token is secret, but series+number is guessable, so the
    rate limit is the control. CAPTCHA is stage 6's."""
    codes = set()
    for _ in range(80):
        result = await client.get(CHECK, params={"series": "А", "number": 1})
        codes.add(result.status_code)
    assert 429 in codes


# --- the card ----------------------------------------------------------------


async def test_the_card_carries_exactly_the_public_fields(
    client: httpx.AsyncClient, active_permit: Permit
):
    """An equality, not a membership: a field added to this response is a field
    published to the whole internet, and it must be a decision rather than the
    by-product of returning one more column."""
    body = (await client.get(CHECK, params={"qr": active_permit.qr_token})).json()
    assert set(body) == CARD_FIELDS
    assert body["valid_from"] == active_permit.period_from.isoformat()
    assert body["valid_to"] == active_permit.period_to.isoformat()


async def test_the_card_is_read_off_the_snapshot_not_off_the_applicant_row(
    db: AsyncSession,
    client: httpx.AsyncClient,
    active_permit: Permit,
    applicant_row: Applicant,
):
    """`tz/05` invariant 7: the snapshot is what the document says, forever. The
    QR page verifies a PRINTED permit, so a holder who renames themselves must
    not make the page disagree with the paper in the inspector's hand — a
    mismatch there reads as a forgery."""
    applicant_row.name = "Ниязов Ниёз Ниёзович"
    await db.commit()

    body = (await client.get(CHECK, params={"qr": active_permit.qr_token})).json()
    assert body["holder"] == "А.***ов А."
    assert body["holder"] != service.mask_name(applicant_row.name)


async def test_the_page_speaks_c12s_four_words(
    db: AsyncSession, client: httpx.AsyncClient, active_permit: Permit
):
    """`tz/04` С12 and `design/03` fix the four words verbatim. The three besides
    «амалда» have no writer until 3.11b, so the column is set here by hand on
    purpose — this test is the contract 3.11b's own transitions will land on."""
    assert (await client.get(CHECK, params={"qr": active_permit.qr_token})).json()[
        "status"
    ] == "амалда"

    for status, word in (
        ("suspended", "тўхтатилган"),
        ("expired", "муддати тугаган"),
        ("revoked", "бекор қилинган"),
    ):
        active_permit.status = status
        await db.commit()
        body = (await client.get(CHECK, params={"qr": active_permit.qr_token})).json()
        assert body["found"] is True, f"{status} is public — it is a permit that existed"
        assert body["status"] == word


# --- the log ------------------------------------------------------------------


async def test_the_channel_says_how_the_permit_was_looked_up(
    db: AsyncSession, client: httpx.AsyncClient, active_permit: Permit
):
    marker = uuid7()
    await client.get(CHECK, params={"qr": active_permit.qr_token})
    await client.get(CHECK, params={"series": active_permit.series, "number": active_permit.number})

    rows = await _rows_since(db, marker)
    assert [row.channel for row in rows] == ["qr", "manual"]
    assert [row.permit_id for row in rows] == [active_permit.id, active_permit.id]


async def test_a_permit_that_is_not_public_is_logged_as_a_miss_with_no_id(
    db: AsyncSession, client: httpx.AsyncClient, issued_permit: Permit
):
    """The log records what was ANSWERED, not what the SELECT happened to find.
    `permit_id IS NULL` is the `not_found` case's whole payload (`models.py`), so
    a permit the page refused to show must not leave its id behind — that row
    would say "somebody checked THIS permit" about a check that was told there is
    nothing to see."""
    marker = uuid7()
    await client.get(CHECK, params={"qr": issued_permit.qr_token})

    rows = await _rows_since(db, marker)
    assert len(rows) == 1
    assert rows[0].result == "not_found"
    assert rows[0].permit_id is None


# --- garbage in, no 500 out ---------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"qr": "Ω"},
        {"qr": "../../etc/passwd"},
        {"qr": "%00"},
        {"qr": "x" * 200},
        {"series": "Ω", "number": 1},
        {"series": "А" * 200, "number": 1},
    ],
    ids=["non_ascii", "traversal", "null_byte", "long_token", "non_ascii_series", "long_series"],
)
async def test_garbage_is_answered_not_found_and_never_500s(
    client: httpx.AsyncClient, params: dict[str, Any]
):
    """The route takes its input straight off a query string a stranger writes.
    Nothing here is compared with `secrets.compare_digest` — it raises on
    non-ASCII operands, and the one route whose stated invariant is "never 500 on
    garbage" is exactly where that has bitten before (lesson)."""
    result = await client.get(CHECK, params=params)
    assert result.status_code in (200, 422), result.text
    if result.status_code == 200:
        assert result.json() == {"found": False}
    else:
        assert result.json()["error"]["code"] == "ERR-VAL-001"


async def test_a_number_wider_than_the_column_is_refused_before_the_database_sees_it(
    client: httpx.AsyncClient,
):
    """`permits.number` is `bigint`; without a bound on the query parameter,
    `?number=10^30` reaches asyncpg and raises out of range — a 500 on an
    anonymous route, handed out for free."""
    result = await client.get(CHECK, params={"series": "А", "number": 10**30})
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "ERR-VAL-001"


@pytest.mark.parametrize(
    "params",
    [{}, {"series": "А"}, {"number": 1}, {"qr": ""}],
    ids=["none", "series", "number", "empty"],
)
async def test_a_request_naming_no_permit_is_refused_and_is_not_counted(
    db: AsyncSession, client: httpx.AsyncClient, params: dict[str, Any]
):
    """Nothing was checked, so nothing is counted — the statistics answer «how
    many permits were verified», and a caller who named none did not verify one.
    A 422 here is no oracle: the boundary is the request's shape, not the data."""
    marker = uuid7()
    result = await client.get(CHECK, params=params)
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "ERR-VAL-001"
    assert await _rows_since(db, marker) == []


async def test_a_permit_whose_snapshot_lost_a_requisite_still_answers(
    db: AsyncSession, active_permit: Permit
):
    """`_snapshot` fills every requisite through `_required`, so an empty one is
    a defect on the ISSUING side — and this page is the wrong place to discover
    it: an unfilled field must print the form's em dash, never surface as a
    `ResponseValidationError` 500 on the one page a citizen with a paper permit
    can reach.

    Called in process, which is also the only direct exercise `service.public_check`
    gets — every test above drives the ROUTE, and a public-surface function whose
    only verification is that it type-checks is how a contract ships untested
    (lesson).
    """
    active_permit.snapshot = {**active_permit.snapshot, "holder_name": "", "leshoz_name": None}
    await db.flush()

    card = await service.public_check(db, qr_token=active_permit.qr_token)
    assert card["found"] is True
    assert card["status"] == "амалда"
    # Not «—.***»: a mask applied to a placeholder is a placeholder masked.
    assert card["holder"] == service.NOT_STATED
    assert card["organization"] == service.NOT_STATED


# --- ruling T5-a: `signatures_valid` is a question about history --------------


async def test_signatures_valid_survives_a_change_to_the_required_set(
    client: httpx.AsyncClient, active_permit: Permit, override_required_signatures
):
    """Ruling T5-a. `permit_required_signatures` is admin-editable and `tz/04`
    С11's «все три обязательны?» is still open with the Agency, so this row WILL
    change. Re-deriving the field from it would make every permit issued before
    that day tell inspectors its signatures do not check out — a lawfully issued
    document reading as suspect, which is the worst thing this page can say.

    The permit is already ACTIVE, so the set that mattered is the one activation
    checked; a fifth purpose added afterwards is a rule for permits issued next.
    """
    await override_required_signatures(
        "permit_head,permit_chief_forester,permit_accountant,permit_recipient,permit_inspector"
    )

    body = (await client.get(CHECK, params={"qr": active_permit.qr_token})).json()
    assert body["status"] == "амалда"
    assert body["signatures_valid"] is True


async def test_a_signature_later_found_invalid_makes_the_card_say_so(
    db: AsyncSession, client: httpx.AsyncClient, active_permit: Permit, head_client
):
    """The other direction, and the one that keeps the field from being a
    constant: `reverify` (3.8's own route, oversight's) writes an invalid
    verdict against a signature whose certificate has since been revoked, and
    the page must report it — while `permits.status` stays «амалда», because
    acting on that discovery is 3.11b's, not this page's.

    The certificate is pushed to `revoked` through the mock adapter's own
    convention (a `REVOKED-` serial prefix), so `reverify` discovers the standing
    the way it would in production rather than having a verdict planted on it.
    """
    signatures = await signatures_service.get_for_object(
        db, object_type=service.OBJECT_TYPE, object_id=active_permit.id
    )
    head = next(row for row in signatures if row.purpose == "permit_head")
    certificate = await signatures_service.get_certificate(db, head.certificate_id)
    certificate.serial_number = f"REVOKED-{certificate.serial_number}"
    await db.flush()

    await signatures_service.reverify(db, signature_id=head.id, user=head_client.user)
    await db.commit()

    body = (await client.get(CHECK, params={"qr": active_permit.qr_token})).json()
    assert body["signatures_valid"] is False, "an oversight downgrade must reach the page"
    assert body["status"] == "амалда", "acting on the downgrade is 3.11b's, not this page's"


async def test_a_failed_signing_attempt_is_evidence_and_not_a_broken_document(
    db: AsyncSession, client: httpx.AsyncClient, active_permit: Permit
):
    """3.8 ruling 8 stores a refused envelope as evidence. A signatory who
    fat-fingers their ERI and retries leaves an invalid row beside their valid
    one, and that must not make a correctly signed permit read as broken —
    which is why the field is computed over the signatures the permit CARRIES,
    not over every row attached to it."""
    signatures = await signatures_service.get_for_object(
        db, object_type=service.OBJECT_TYPE, object_id=active_permit.id
    )
    head = next(row for row in signatures if row.purpose == "permit_head")
    db.add(
        Signature(
            object_type=service.OBJECT_TYPE,
            object_id=active_permit.id,
            purpose="permit_head",
            signer_user_id=head.signer_user_id,
            certificate_id=head.certificate_id,
            doc_hash=head.doc_hash,
            signature_value="a fat-fingered envelope",
            signed_at=head.signed_at,
            verification={"reason": "signature_invalid"},
            verification_status="invalid",
        )
    )
    await db.commit()

    body = (await client.get(CHECK, params={"qr": active_permit.qr_token})).json()
    assert body["signatures_valid"] is True


# --- review round 1: the route is the system's first open door ----------------


async def test_the_two_channels_do_not_share_one_budget(db: AsyncSession, active_permit: Permit):
    """Review I1. `permit_counters` hands out `last_number + 1`, so series+number
    is a GAPLESS space a script can walk, while `qr_token` is 32 random bytes.
    With one bucket for both, a scanner exhausting the guessable path also 429s
    the citizen scanning a printed code off the same egress IP — and a NAT is one
    egress IP for a whole region. Each direction is asserted: neither channel may
    starve the other.
    """
    app = create_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as anonymous:
        async with app.router.lifespan_context(app):
            await db.commit()
            manual = {"series": active_permit.series, "number": active_permit.number}
            qr = {"qr": active_permit.qr_token}

            codes = {(await anonymous.get(CHECK, params=manual)).status_code for _ in range(40)}
            assert 429 in codes, "the typed path is the tighter budget and must run out first"
            assert (await anonymous.get(CHECK, params=qr)).status_code == 200, (
                "an exhausted typed budget must not starve a printed QR code"
            )

            ratelimit.reset()
            for _ in range(200):
                if (await anonymous.get(CHECK, params=qr)).status_code == 429:
                    break
            else:
                raise AssertionError("the qr channel is not limited at all")
            assert (await anonymous.get(CHECK, params=manual)).status_code == 200


async def test_the_rate_limiter_does_not_leak_a_bucket_per_visitor(
    db: AsyncSession, active_permit: Permit, monkeypatch: pytest.MonkeyPatch
):
    """Review I3. Until this route, every limited scope was reached only by
    someone who had found a login form or been handed a webhook secret. A page
    advertised on printed documents lets anybody on the internet mint one dict
    entry per source address — unlimited over IPv6, never freed.

    Driven through the real route with real distinct client addresses, not by
    calling a sweep helper: a cap nothing invokes is not a cap.
    """
    monkeypatch.setattr(ratelimit, "MAX_BUCKETS", 8)
    app = create_app()
    async with app.router.lifespan_context(app):
        await db.commit()
        for octet in range(32):
            transport = httpx.ASGITransport(
                app=app, raise_app_exceptions=False, client=(f"10.0.0.{octet}", 5000)
            )
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as visitor:
                assert (await visitor.get(CHECK, params={"qr": "nope"})).status_code == 200
        assert len(ratelimit._buckets) <= ratelimit.MAX_BUCKETS, (
            "32 visitors must not leave 32 permanent buckets behind"
        )

        # The other half: a bucket that has refilled is dropped on its own,
        # without waiting for the cap. Refilled means idle for a full minute,
        # shortened here rather than slept through.
        monkeypatch.setattr(ratelimit, "MAX_BUCKETS", 20_000)
        monkeypatch.setattr(ratelimit, "BUCKET_IDLE_SECONDS", 0.0)
        monkeypatch.setattr(ratelimit, "SWEEP_EVERY_SECONDS", 0.0)
        ratelimit.reset()
        first = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=("10.1.1.1", 1))
        async with httpx.AsyncClient(transport=first, base_url="http://t") as visitor:
            await visitor.get(CHECK, params={"qr": "nope"})
        assert len(ratelimit._buckets) == 1
        second = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=("10.2.2.2", 1))
        async with httpx.AsyncClient(transport=second, base_url="http://t") as visitor:
            await visitor.get(CHECK, params={"qr": "nope"})
        assert [ip for _, ip in ratelimit._buckets] == ["10.2.2.2"], (
            "the first visitor's refilled bucket must have been swept, not kept forever"
        )


def test_the_public_checks_access_log_line_is_dropped():
    """Review I2. `qr_check_log` records no IP and no personal data by
    `design/02`'s instruction — and uvicorn's access log would undo that from
    outside the application, writing the visitor's address AND the printed token
    (`?qr=<43 chars>`) at INFO into a file no purge job covers.

    The record is built the way uvicorn builds one: `args = (client_addr, method,
    full_path, http_version, status)`.
    """
    configure_logging("json")
    access = logging.getLogger("uvicorn.access")
    installed = [f for f in access.filters if isinstance(f, _SilentPathFilter)]
    assert len(installed) == 1, "configure_logging installs it exactly once, however often it runs"
    configure_logging("json")
    assert len([f for f in access.filters if isinstance(f, _SilentPathFilter)]) == 1

    def record(path: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=("203.0.113.7:51234", "GET", path, "1.1", 200),
            exc_info=None,
        )

    assert installed[0].filter(record(f"{CHECK}?qr=THE-PRINTED-TOKEN")) is False
    assert installed[0].filter(record(f"{CHECK}?series=A&number=1")) is False
    assert installed[0].filter(record("/api/v1/permits/x/signatures")) is True
    assert installed[0].filter(record("/health/ready")) is True
