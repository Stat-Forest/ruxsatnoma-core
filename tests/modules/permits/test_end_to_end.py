"""The one run that proves the system does what it exists for, and the public
surface stage 4 builds against.

**Two kinds of test, deliberately, in this order.** The end-to-end scenario
drives HTTP and proves the ROUTES work. Everything under "the in-process public
surface" calls `service.get` / `for_application` / `pdf_bytes` / `set_status` /
the two providers DIRECTLY, the way `inspections` (4.1), `oversight` (4.2) and
`archive` (4.7) will call them — because an HTTP scenario exercises no
in-process function at all, and 3.7's own Task 8 shipped `effective_norm` and
`run_checks` reached by nothing while its end-to-end test stayed green (lesson:
a "public surface" task's own end-to-end test can ship the surface untested).

Three deviations from the plan's verbatim snippets, each forced by something
that landed after the plan was written:

  * the 4th signature is `holder_client`, not the re-exported `applicant_client`
    — that fixture is an applicant unrelated to any application here, so it
    would be refused as `not_the_holder`, and `test_issue.py` needs it to stay
    that way (Task 4's own note in `test_signatures.py`);
  * the ERI identity travels on the `Signer` fixture instead of the plan's fixed
    PINFLs and certificate serials, which collide on the second run against this
    shared, persistent test database (`Signer`'s docstring);
  * the closing assertion reads the application through
    `applications.service.get` rather than `GET /api/v1/applications/{id}`:
    `applications` has no `router.py` on this branch, so that route does not
    exist yet (controller ruling P2). It proves the same fact — and it must go
    through `_reread`, because `service.get` is `db.get` underneath and returns
    the stale in-identity-map row without emitting a SELECT (lesson).
"""

import asyncio
import hashlib
import uuid
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.errors import DomainError
from app.db import make_session_factory
from app.modules.admin.models import ClassifierItem
from app.modules.applications import service as applications_service
from app.modules.applications.models import Application
from app.modules.audit.models import AuditLog
from app.modules.notifications.models import NotificationTemplate
from app.modules.notifications.service import DEFAULT_CHANNELS
from app.modules.permits import events, jobs, service, signers
from app.modules.permits.models import PERMIT_STATUSES, Permit, PermitStatusHistory
from app.modules.signatures import service as signatures_service
from tests.modules.permits.conftest import Signer, sign_permit

API = "/api/v1"


async def _reread[T](db: AsyncSession, row: T) -> T:
    """Re-read `row` before asserting on it — `test_signatures.py::_reread`'s own
    helper, kept per-file the way this suite keeps its helpers. The fixtures
    build rows on `db` while the requests under test run on the app's session,
    and `expire_on_commit=False` means `db` never notices (lesson)."""
    await db.refresh(row)
    return row


# --- the end-to-end run ------------------------------------------------------


async def test_a_paid_application_becomes_a_publicly_verifiable_permit(
    db: AsyncSession,
    client: httpx.AsyncClient,
    hodim_client: httpx.AsyncClient,
    head_client: Signer,
    chief_forester_client: Signer,
    accountant_client: Signer,
    holder_client: Signer,
    paid_application: Application,
):
    """PAID -> issued -> 3+1 signed -> ACTIVE -> anybody can check it. If this
    passes, the thing the whole system exists to produce works."""
    issued = await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    assert issued.status_code == 201, issued.text
    permit_id = uuid.UUID(issued.json()["id"])
    assert issued.json()["status"] == "pending_signatures"

    # The holder can already read and download the document; it is just not in
    # force yet. `pdf` is the exact bytes every signature below is taken over.
    pdf_response = await holder_client.client.get(f"{API}/permits/{permit_id}/pdf")
    assert pdf_response.status_code == 200, pdf_response.text
    pdf = pdf_response.content
    assert pdf.startswith(b"%PDF")

    # Ruling 18: the application reaches PERMIT_ISSUED when the permit becomes
    # ACTIVE, never at issuance — tz/05 defines that status as «сформировано
    # **и подписано**». Read through `applications.service.get`, the in-process
    # accessor 3.11 is allowed to use (controller ruling P2), and then refreshed:
    # `get` is `db.get` underneath and hands back the stale identity-map row
    # without emitting a SELECT at all (lesson).
    application = await applications_service.get(db, paid_application.id)
    assert application is not None
    assert (await _reread(db, application)).status == "PAID"

    for signer, purpose in (
        (head_client, "permit_head"),
        (chief_forester_client, "permit_chief_forester"),
        (accountant_client, "permit_accountant"),
        (holder_client, signers.RECIPIENT_PURPOSE),
    ):
        result = await sign_permit(signer, permit_id, purpose, pdf)
        assert result.status_code == 200, result.text

    card = await holder_client.client.get(f"{API}/permits/{permit_id}")
    assert card.status_code == 200, card.text
    body = card.json()
    assert body["status"] == "active"
    assert body["issued_at"] is not None
    assert len(body["signatures"]) == 4
    assert body["missing_signatures"] == []
    # (None -> pending_signatures) at issuance, (pending_signatures -> active)
    # at the fourth signature.
    assert [(row["from_status"], row["to_status"]) for row in body["history"]] == [
        (None, "pending_signatures"),
        ("pending_signatures", "active"),
    ]
    # Never on any read shape: `qr_token` is the key to the anonymous page and
    # `snapshot` is what the PDF already says (schemas.py).
    assert "qr_token" not in body
    assert "snapshot" not in body

    permit = await service.get(db, permit_id)
    assert permit is not None
    public = await client.get(f"{API}/public/permits/check", params={"qr": permit.qr_token})
    assert public.status_code == 200, public.text
    assert public.json()["found"] is True
    assert public.json()["status"] == "амалда"
    assert public.json()["signatures_valid"] is True

    application = await applications_service.get(db, paid_application.id)
    assert application is not None
    assert (await _reread(db, application)).status == "PERMIT_ISSUED"


# --- the transition table ----------------------------------------------------


def test_the_transition_table_covers_every_status_and_only_real_targets() -> None:
    """The one-source-of-truth discipline `models.py` describes for the CHECK
    constraints, and `applications.service`'s own table already carries: a
    status added to `PERMIT_STATUSES` and not here, or a typo'd target, fails
    before it ships."""
    assert set(service.PERMIT_TRANSITIONS) == set(PERMIT_STATUSES)
    for status, targets in service.PERMIT_TRANSITIONS.items():
        assert targets <= set(PERMIT_STATUSES), status


def test_no_status_transitions_to_itself() -> None:
    """`tz/05` has no self-loop, so a repeat call for a status the permit
    already holds is exactly as illegal as any other jump — which is how a
    retrying caller tells "already applied, harmless" (`from == to`) from a
    genuine mistake, the same signal `applications.set_status` gives."""
    for status, targets in service.PERMIT_TRANSITIONS.items():
        assert status not in targets


def test_archived_is_the_only_terminal_status() -> None:
    terminal = [status for status, targets in service.PERMIT_TRANSITIONS.items() if not targets]
    assert terminal == ["archived"]


def test_the_edges_this_stage_writes_outside_set_status_are_in_the_table() -> None:
    """`_activate` and `jobs.expire_permits` write their own transitions (they
    do more than move a status — see the public-surface comment), so the table
    would describe the code only by accident unless their two edges are asserted
    against it here."""
    assert service.ACTIVE_STATUS in service.PERMIT_TRANSITIONS[service.INITIAL_STATUS]
    assert jobs.EXPIRED_STATUS in service.PERMIT_TRANSITIONS[service.ACTIVE_STATUS]


# --- the in-process public surface: what stage 4 calls ------------------------


async def test_get_returns_the_row_and_none_for_an_unknown_id(
    db: AsyncSession, issued_permit: Permit
):
    """`service.get` — the level-5 read, with no permission and no zone rule of
    its own (`gis.service.published_version`'s shape). An unknown id is `None`,
    not a raise: the caller is another service and decides for itself whether a
    missing permit is an error."""
    found = await service.get(db, issued_permit.id)
    assert found is not None
    assert found.id == issued_permit.id
    assert await service.get(db, uuid.uuid4()) is None


async def test_for_application_is_the_one_to_one_partner_of_the_application(
    db: AsyncSession, issued_permit: Permit, second_paid_application: Application
):
    partner = await service.for_application(db, issued_permit.application_id)
    assert partner is not None
    assert partner.id == issued_permit.id
    # An application that has not been issued a permit yet has none.
    assert await service.for_application(db, second_paid_application.id) is None


async def test_pdf_bytes_is_the_stored_document_and_never_a_re_render(
    db: AsyncSession, issued_permit: Permit, permit_pdf: bytes
):
    """Ruling 3: one document, hashed once. Two calls must hand back the same
    bytes, and those bytes must be the ones `doc_hash` was taken over."""
    again = await service.pdf_bytes(db, issued_permit.id)
    assert again == permit_pdf
    assert hashlib.sha256(again).hexdigest() == issued_permit.doc_hash


async def test_pdf_bytes_raises_for_an_unknown_permit(db: AsyncSession):
    with pytest.raises(DomainError) as raised:
        await service.pdf_bytes(db, uuid.uuid4())
    assert raised.value.code == "ERR-SYS-003"


async def test_set_status_refuses_an_illegal_jump(db: AsyncSession, issued_permit: Permit):
    """The method 3.11b and 4.7 move a permit with. A jump `tz/05` does not
    allow is refused HERE, not caught by review two stages later: a permit
    awaiting signatures never came into force, so it cannot expire."""
    with pytest.raises(DomainError) as raised:
        await service.set_status(db, issued_permit.id, to_status="expired")
    assert raised.value.code == "ERR-PERM-001"
    assert raised.value.details == {
        "reason": "bad_transition",
        "from": "pending_signatures",
        "to": "expired",
    }
    assert (await _reread(db, issued_permit)).status == "pending_signatures"


async def test_set_status_refuses_a_repeat_of_the_status_already_held(
    db: AsyncSession, active_permit: Permit
):
    """`from == to` is the signal a retrying caller reads as "already applied"."""
    with pytest.raises(DomainError) as raised:
        await service.set_status(db, active_permit.id, to_status="active")
    assert raised.value.details == {"reason": "bad_transition", "from": "active", "to": "active"}


async def test_set_status_raises_for_an_unknown_permit(db: AsyncSession):
    with pytest.raises(DomainError) as raised:
        await service.set_status(db, uuid.uuid4(), to_status="suspended")
    assert raised.value.code == "ERR-SYS-003"


async def test_set_status_moves_the_permit_and_leaves_a_history_row_and_a_trail(
    db: AsyncSession, active_permit: Permit, head_client: Signer
):
    """3.11b's own move, made through the one function it is allowed to use."""
    moved = await service.set_status(
        db,
        active_permit.id,
        to_status="suspended",
        actor=head_client.user,
        reason="ВМҚ 689, 4-банд",
    )
    assert moved.status == "suspended"

    history = (
        await db.scalars(
            select(PermitStatusHistory)
            .where(PermitStatusHistory.permit_id == active_permit.id)
            .order_by(PermitStatusHistory.occurred_at, PermitStatusHistory.id)
        )
    ).all()
    assert [(row.from_status, row.to_status) for row in history] == [
        (None, "pending_signatures"),
        ("pending_signatures", "active"),
        ("active", "suspended"),
    ]
    assert history[-1].changed_by == head_client.user.id
    assert history[-1].legal_basis == "ВМҚ 689, 4-банд"

    entry = (
        await db.scalars(
            select(AuditLog)
            .where(
                AuditLog.object_id == active_permit.id,
                AuditLog.action == service.PERMIT_STATUS_CHANGE,
            )
            .order_by(AuditLog.occurred_at.desc())
        )
    ).first()
    assert entry is not None
    assert entry.user_id == head_client.user.id
    assert entry.old_value == {"status": "active"}
    assert entry.new_value == {"status": "suspended"}
    assert entry.basis == "ВМҚ 689, 4-банд"


async def test_set_status_walks_the_whole_tail_of_the_lifecycle(
    db: AsyncSession, active_permit: Permit
):
    """Every status 3.11b and 4.7 will write, in one pass — a target this
    function cannot reach is a stage that will have to change a contract this
    task promised would not change. No actor: `changed_by` is nullable exactly
    because `expired`/`archived` are the system's, not a person's."""
    # `.status` alone was a truthiness check on a non-empty string: every one of the
    # six statuses passes it, and so does a function that ignores `to_status`
    # entirely (final fix wave).
    assert (
        await service.set_status(db, active_permit.id, to_status="suspended")
    ).status == "suspended"
    assert (await service.set_status(db, active_permit.id, to_status="revoked")).status == "revoked"
    final = await service.set_status(db, active_permit.id, to_status="archived")
    assert final.status == "archived"
    with pytest.raises(DomainError):
        await service.set_status(db, active_permit.id, to_status="active")


async def test_set_status_locks_the_permit_so_two_callers_cannot_race(
    db: AsyncSession, engine: AsyncEngine, active_permit: Permit
):
    """The lesson a single-caller write path is written under: a function
    documented as "the ONE way" locks its row. 3.11b's revoke and 4.7's archive
    are two real callers that can arrive together.

    Two REAL sessions (`applications/test_public_surface.py`'s own template) —
    a single session cannot demonstrate a row lock against itself — and the
    permit must be COMMITTED first, since another connection cannot see
    uncommitted work.
    """
    await db.commit()
    permit_id = active_permit.id

    factory = make_session_factory(engine)
    session_a = factory()
    session_b = factory()
    try:
        moved = await service.set_status(session_a, permit_id, to_status="revoked")
        assert moved.status == "revoked"

        task = asyncio.create_task(service.set_status(session_b, permit_id, to_status="suspended"))
        await asyncio.sleep(0.3)  # generous headroom for a localhost query
        assert not task.done(), "session_b should still be blocked on session_a's row lock"

        await session_a.commit()  # releases the lock

        with pytest.raises(DomainError) as raised:
            await asyncio.wait_for(task, timeout=5)
        # Unblocked, session_b re-reads the committed `revoked` rather than the
        # stale `active` it would have seen without the lock — so the illegal
        # jump is refused instead of silently overwriting session_a's write.
        assert raised.value.code == "ERR-PERM-001"
        assert raised.value.details == {
            "reason": "bad_transition",
            "from": "revoked",
            "to": "suspended",
        }
        assert (await _reread(db, active_permit)).status == "revoked"
    finally:
        await session_a.rollback()
        await session_b.rollback()
        await session_a.close()
        await session_b.close()


async def test_missing_signatures_answers_in_process_and_empties_as_they_land(
    db: AsyncSession,
    issued_permit: Permit,
    permit_pdf: bytes,
    head_client: Signer,
    chief_forester_client: Signer,
    accountant_client: Signer,
    holder_client: Signer,
):
    """Entry point #7 of the frozen block, called as a caller calls it rather
    than read off a JSON key — the six above got a direct call and this one was
    reached only through `body["missing_signatures"]`, which is the exact shape
    the lesson names ("an HTTP scenario exercises the ROUTES, not the in-process
    functions a future module will call").

    Order matters and is asserted: the list is the CONFIGURED display order, so
    it must not reorder itself as purposes drop out of it (plan ruling 5 — a UI
    must not read the first entry as "whose turn it is", but it may rely on the
    order being stable)."""
    before = await service.missing_signatures(db, issued_permit.id)
    assert before == await signatures_service.required_purposes(db, service.OBJECT_TYPE)
    assert signers.RECIPIENT_PURPOSE in before

    for signer, purpose in (
        (head_client, "permit_head"),
        (chief_forester_client, "permit_chief_forester"),
        (accountant_client, "permit_accountant"),
    ):
        assert (await sign_permit(signer, issued_permit.id, purpose, permit_pdf)).status_code == 200
        remaining = await service.missing_signatures(db, issued_permit.id)
        assert purpose not in remaining
        assert remaining == [one for one in before if one in remaining]  # order preserved

    assert await service.missing_signatures(db, issued_permit.id) == [signers.RECIPIENT_PURPOSE]
    assert (
        await sign_permit(holder_client, issued_permit.id, signers.RECIPIENT_PURPOSE, permit_pdf)
    ).status_code == 200
    assert await service.missing_signatures(db, issued_permit.id) == []
    # An object nobody has ever signed still answers the full requirement set,
    # never `[]` — "nothing signed" and "nothing required" must not look alike.
    assert await service.missing_signatures(db, uuid.uuid4()) == before


async def test_the_two_provider_seams_answer_in_process(db: AsyncSession, active_permit: Permit):
    """Task 6's functions, called the way `gis` and `norms` call them rather
    than through the seam list — the same "does the contract function actually
    run" check every entry above gets."""
    occupied = await service.occupancy_provider(db, [active_permit.contour_id])
    assert occupied[active_permit.contour_id] == active_permit.area_ha
    assert await service.occupancy_provider(db, []) == {}

    load = await service.load_provider(
        db, active_permit.contour_id, active_permit.period_from, active_permit.period_to
    )
    assert load == (active_permit.sb_load or Decimal("0"))
    # A window that ends the day before the permit begins overlaps nothing.
    before = active_permit.period_from - timedelta(days=30)
    assert await service.load_provider(
        db, active_permit.contour_id, before, active_permit.period_from - timedelta(days=1)
    ) == Decimal("0")


async def test_set_status_records_a_suspensions_legal_ground_and_the_run_it_came_from(
    db: AsyncSession, active_permit: Permit, head_client: Signer
):
    """Ruling T8-b: the three arguments past `reason` are what keeps "the ONE way
    a module moves a permit" from being a rule 3.11b has to break on its first
    write. `permit_status_history` already HAS the two columns; before this they
    were unreachable through the only function allowed to write the row.

    `reason_item_id` is a real `classifier_items` row, not a fabricated uuid: the
    FK is closed, and a made-up id would prove the argument is accepted while
    hiding that it can never be stored (the shape migration 0015 caught in
    `norms`' own tests)."""
    ground = await db.scalar(select(ClassifierItem).limit(1))
    assert ground is not None, "0005 seeds classifier items; none found"

    moved = await service.set_status(
        db,
        active_permit.id,
        to_status="suspended",
        actor=head_client.user,
        reason="ВМҚ 689, 4-банд",
        reason_item_id=ground.id,
        correlation_id="job:t8b",
    )
    assert moved.status == "suspended"

    row = (
        await db.scalars(
            select(PermitStatusHistory)
            .where(PermitStatusHistory.permit_id == active_permit.id)
            .order_by(PermitStatusHistory.occurred_at.desc(), PermitStatusHistory.id.desc())
        )
    ).first()
    assert row is not None
    assert (row.to_status, row.reason_item_id, row.legal_basis) == (
        "suspended",
        ground.id,
        "ВМҚ 689, 4-банд",
    )

    entry = (
        await db.scalars(
            select(AuditLog)
            .where(
                AuditLog.object_id == active_permit.id,
                AuditLog.action == service.PERMIT_STATUS_CHANGE,
            )
            .order_by(AuditLog.occurred_at.desc())
        )
    ).first()
    assert entry is not None
    assert entry.correlation_id == "job:t8b"
    # The CODE, so a report can group suspensions by cause — `basis` beside it is
    # the free text a citizen reads, and the two are not alternatives.
    assert entry.extra == {"reason_item_id": str(ground.id)}


async def test_set_status_defaults_leave_every_new_column_null(
    db: AsyncSession, active_permit: Permit
):
    """The widening must not change the narrow call: 4.7 archives with a status
    and nothing else, and its history row must not acquire a phantom ground."""
    await service.set_status(db, active_permit.id, to_status="revoked")
    row = (
        await db.scalars(
            select(PermitStatusHistory)
            .where(PermitStatusHistory.permit_id == active_permit.id)
            .order_by(PermitStatusHistory.occurred_at.desc(), PermitStatusHistory.id.desc())
        )
    ).first()
    assert row is not None
    assert (row.reason_item_id, row.doc_file_id, row.legal_basis, row.changed_by) == (
        None,
        None,
        None,
        None,
    )


# --- the guard ruling 17 exists for ------------------------------------------


async def test_every_event_this_module_notifies_on_has_a_template(db: AsyncSession):
    """Ruling 17: with no template, `notify()` writes a raw fallback string
    in-app and sends NOTHING by SMS or e-mail — silently, with a green test on
    top asserting "a notification row exists".

    EVERY default channel, not just `inapp`. This checked `inapp` alone until
    2026-09-03, which is precisely the half that fails LOUDLY (a visible
    fallback string in the cabinet): an unseeded `sms` row is the silent one —
    the message is simply never sent — and it would have walked straight past
    the guard written to catch it. The channels come from
    `notifications.service.DEFAULT_CHANNELS`, so a third one added there is
    covered here without anybody remembering to."""
    missing = []
    for event_code in events.NOTIFIED_EVENT_CODES:
        for channel in DEFAULT_CHANNELS:
            row = await db.scalar(
                select(NotificationTemplate).where(
                    NotificationTemplate.event_code == event_code,
                    NotificationTemplate.channel == channel,
                    NotificationTemplate.status == "active",
                )
            )
            if row is None:
                missing.append(f"{event_code}/{channel}")
    assert DEFAULT_CHANNELS, "no default channels — the loop would assert nothing"
    assert not missing, f"no active notification template for: {missing}"


def test_the_notified_set_is_exactly_what_this_module_can_send() -> None:
    """`permit.expiring` is seeded and nothing sends it yet — the reminder job
    is a later stage's. Listed here on purpose: the set is what a template must
    exist for, not what fired last night, and dropping the unsent one would let
    the reminder ship with no text (recorded, not a defect)."""
    assert set(events.NOTIFIED_EVENT_CODES) == {
        "permit.issued",
        "permit.signed",
        "permit.active",
        "permit.expiring",
        "permit.expired",
        "permit.due",
    }
