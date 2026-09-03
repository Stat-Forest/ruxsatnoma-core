"""Issuance: a PAID application becomes a numbered, rendered, hash-frozen permit.

The brief's own six tests, with one rewrite: it asserted the application's status
through `GET /api/v1/applications/{id}`, a route that does not exist —
`app/modules/applications/` ships no `router.py` on `dev`, and nothing reaches an
application through the API until 3.9a branch 2 lands. The fact under test is the
same either way, so it is read through `applications.service.get`.

`number` is asserted as "the counter's previous value plus one", never as the
literal 1 the brief wrote: the counter row is committed, shared and persistent, so
a literal passes exactly once per freshly migrated database (lesson: the test DB is
shared, persistent and never empty — including the spot you picked).
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, publish
from app.event_subscriptions import PAYMENT_CONFIRMED
from app.modules.admin.models import District, Organization, Region
from app.modules.applications import service as applications_service
from app.modules.applications.models import Application, ApplicationItem
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Applicant, User
from app.modules.norms.models import Calculation
from app.modules.notifications.models import Notification
from app.modules.permits import events, render, repo, service
from app.modules.permits.models import Permit, PermitStatusHistory, PermitTemplate
from tests.modules.permits.conftest import STORED_LAYOUT, make_paid_application

API = "/api/v1"


async def counter(db: AsyncSession, series: str = "А") -> int:
    """The counter's CURRENT value, read on the test's own session. Committed by
    every issuance, so the only stable assertion about `number` is relative."""
    value = await db.scalar(
        text("SELECT last_number FROM permit_counters WHERE series = :s").bindparams(s=series)
    )
    assert value is not None
    return value


async def test_issuing_from_a_paid_application_produces_a_numbered_document(
    db: AsyncSession, hodim_client, paid_application: Application
):
    before = await counter(db)

    result = await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    assert result.status_code == 201, result.text

    body = result.json()
    assert body["status"] == "pending_signatures"
    assert body["series"] == "А"
    assert body["number"] == before + 1
    assert body["pdf_file_id"] is not None
    assert "qr_token" not in body, "ruling 8: the token is a secret, never in a response body"


async def test_the_stored_hash_is_the_hash_of_the_stored_pdf(
    db: AsyncSession, hodim_client, paid_application: Application
):
    """Ruling 3: every signature is taken over these exact bytes, so the hash
    on the row and the bytes in storage can never be allowed to drift."""
    await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    permit = await service.for_application(db, paid_application.id)
    assert permit is not None
    pdf = await service.pdf_bytes(db, permit.id)

    assert permit.doc_hash == hashlib.sha256(pdf).hexdigest()


async def test_an_unpaid_application_is_refused_and_recorded_as_ri_10(
    db: AsyncSession, hodim_client, approved_application: Application
):
    """tz/04 С11 + tz/10: RI-10 is CRITICAL and immediate. The refusal must
    survive the exception that explains it, so it commits before raising."""
    result = await hodim_client.post(f"{API}/applications/{approved_application.id}/permit")
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "ERR-PAY-001"

    entries = (
        await db.scalars(select(AuditLog).where(AuditLog.object_id == approved_application.id))
    ).all()
    assert any((e.extra or {}).get("risk_indicator") == "RI-10" for e in entries)
    assert all(e.result == "denied" for e in entries)
    assert not (
        await db.scalars(select(Permit).where(Permit.application_id == approved_application.id))
    ).all()


async def test_a_second_issuance_for_the_same_application_is_refused(
    hodim_client, paid_application: Application
):
    """design/02: application_id is unique. Caught as a domain answer, not as
    an IntegrityError the applicant reads as 'issuance failed'."""
    first = await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    assert first.status_code == 201

    second = await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ERR-PERM-001"


async def test_issuance_leaves_the_application_paid_until_it_is_signed(
    db: AsyncSession, hodim_client, paid_application: Application
):
    """Ruling 18: tz/05 defines PERMIT_ISSUED as «сформировано И ПОДПИСАНО».
    A permit needs four different signatories and may sit unsigned for days —
    the application must not claim otherwise. Task 4 owns the move.

    Read through the module's own public surface rather than through
    `GET /applications/{id}`, which does not exist yet (see the module docstring)."""
    await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")

    application = await applications_service.get(db, paid_application.id)
    assert application is not None
    await db.refresh(application)
    assert application.status == "PAID"


async def test_the_snapshot_survives_the_applicant_being_renamed(
    db: AsyncSession, hodim_client, paid_application: Application, applicant_user: User
):
    """tz/05 invariant 7: changing user data never changes an issued document."""
    await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    permit = await service.for_application(db, paid_application.id)
    assert permit is not None
    before = permit.snapshot["holder_name"]

    applicant_user.full_name = "Совершенно Другое Имя"
    await db.flush()
    await db.refresh(permit)

    assert permit.snapshot["holder_name"] == before


# --- beyond the brief ---------------------------------------------------------


async def test_the_snapshot_survives_the_applicant_ROW_being_renamed(
    db: AsyncSession, hodim_client, paid_application: Application, applicant_row: Applicant
):
    """The test above renames the USER; `holder_name` is copied from the
    `applicants` row, so on its own it would pass even if the snapshot were
    re-derived on every read. This one renames the actual source."""
    await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    permit = await service.for_application(db, paid_application.id)
    assert permit is not None
    assert permit.snapshot["holder_name"] == applicant_row.name

    applicant_row.name = "Бошқа Мутлақо Исм"
    await db.flush()
    await db.refresh(permit)

    assert permit.snapshot["holder_name"] != applicant_row.name


async def test_a_template_with_no_layout_file_renders_the_bundled_layout(
    db: AsyncSession, hodim_client, paid_application: Application
):
    """Migration 0019 seeds the grazing template with a NULL `layout_file_id`,
    which means "the layout bundled in `app/modules/permits/assets/`" (task 1,
    decision 2). Byte-compared against a fresh render from that very file, so this
    proves WHICH layout was used and that the PDF was made from the stored
    snapshot — not merely that some PDF came out."""
    await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    permit = await service.for_application(db, paid_application.id)
    assert permit is not None

    template = await db.get(PermitTemplate, permit.template_id)
    assert template is not None
    assert template.layout_file_id is None

    expected = render.render_permit(
        permit.snapshot, render.default_layout(), service.qr_url(permit.qr_token)
    )
    assert await service.pdf_bytes(db, permit.id) == expected


async def test_a_template_with_a_stored_layout_is_rendered_from_that_file(
    db: AsyncSession,
    hodim_client,
    apiary_paid_application: Application,
    apiary_template: PermitTemplate,
):
    """The other arm: a non-null `layout_file_id` means "fetch those bytes". The
    stored layout omits `{{ sb_load }}`, which is exactly right for an apiary —
    `sb_load` is null there and the bundled grazing layout would refuse it."""
    result = await hodim_client.post(f"{API}/applications/{apiary_paid_application.id}/permit")
    assert result.status_code == 201, result.text

    permit = await service.for_application(db, apiary_paid_application.id)
    assert permit is not None
    assert permit.template_id == apiary_template.id
    assert permit.sb_load is None

    expected = render.render_permit(permit.snapshot, STORED_LAYOUT, service.qr_url(permit.qr_token))
    assert await service.pdf_bytes(db, permit.id) == expected


async def test_the_snapshot_carries_the_calculation_that_was_priced(
    db: AsyncSession, hodim_client, paid_application: Application
):
    """Ruling 19: without the id, a recalculation between approval and issuance
    could print a figure the citizen never paid, and nothing on the permit's side
    would show it. A guard on another branch is not evidence; the id is."""
    calculation = await applications_service.current_calculation(db, paid_application.id)
    assert calculation is not None

    await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    permit = await service.for_application(db, paid_application.id)
    assert permit is not None

    assert permit.snapshot["calculation_id"] == str(calculation.id)
    assert permit.amount == calculation.amount


async def test_issuance_without_a_calculation_is_refused_before_a_number_is_taken(
    db: AsyncSession, hodim_client, unpriced_application: Application
):
    """`tz/13` field 18 (the permit's total) has one source. A missing one is a
    loud refusal at step 3, BEFORE the counter is touched — a burnt series number
    is a gap in a legal register nobody can explain later, and `permit_counters`
    has no way to give one back."""
    before = await counter(db)

    result = await hodim_client.post(f"{API}/applications/{unpriced_application.id}/permit")

    assert result.status_code == 422
    assert result.json()["error"]["code"] == "ERR-VAL-001"
    assert result.json()["error"]["details"]["reason"] == "no_calculation"
    assert await counter(db) == before


# --- what this module can verify about the price on its own -------------------
# `payments.issue_invoice` and `service.issue` each read "the newest calculation
# for this application" and, being both level 4, cannot compare notes: an audit
# probe had the permit print 9 999 999,00 against a paid 2 060 000,00 invoice.
# "The printed amount is the billed amount" is NOT answerable from here — the
# invoice lives in `payments`, which this module may not read — so the two checks
# below are what is local. `tests/test_cross_module_journey.py` carries the
# money-level story and the pin on the accident that keeps the hole unreachable.


async def test_a_calculation_priced_for_another_plot_is_refused(
    db: AsyncSession,
    hodim_client,
    paid_application: Application,
    second_paid_application: Application,
):
    """The permit prints the contour, the activity and the money side by side —
    the first two off the APPLICATION, the last off the CALCULATION. A newer
    calculation priced for a different plot would make the document contradict
    itself, so issuance refuses rather than printing it."""
    before = await counter(db)
    other = await applications_service.current_calculation(db, second_paid_application.id)
    assert other is not None and other.contour_id != paid_application.contour_id
    db.add(
        Calculation(
            application_id=paid_application.id,
            contour_id=other.contour_id,
            activity_type_id=other.activity_type_id,
            rule_code_version=other.rule_code_version,
            input_snapshot=other.input_snapshot,
            used_sb=other.used_sb,
            amount=other.amount,
            breakdown=other.breakdown,
        )
    )
    await db.flush()

    result = await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")

    assert result.status_code == 422, result.text
    assert result.json()["error"]["details"]["reason"] == "calculation_for_another_subject"
    assert await counter(db) == before


async def test_a_calculation_made_after_the_decision_is_refused(
    db: AsyncSession, hodim_client, paid_application: Application
):
    """The decision is what froze the price, so a calculation created after it
    cannot be the one that was invoiced — 3.9b's own ruling ("no recalculation
    from APPROVED onwards") enforced a second time at the point the number
    becomes a printed document.

    Both timestamps are explicit: Postgres' `now()` is the TRANSACTION's clock,
    so every row this test writes would otherwise share one instant and the
    comparison would prove nothing."""
    before = await counter(db)
    priced = await applications_service.current_calculation(db, paid_application.id)
    assert priced is not None
    decided_at = datetime.now(UTC)
    paid_application.decided_at = decided_at
    db.add(
        Calculation(
            application_id=paid_application.id,
            contour_id=priced.contour_id,
            activity_type_id=priced.activity_type_id,
            rule_code_version=priced.rule_code_version,
            input_snapshot=priced.input_snapshot,
            used_sb=priced.used_sb,
            amount=Decimal("9999999.00"),
            breakdown={"total": "9999999.00"},
            created_at=decided_at + timedelta(minutes=5),
        )
    )
    await db.flush()

    result = await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")

    assert result.status_code == 422, result.text
    assert result.json()["error"]["details"]["reason"] == "calculation_after_decision"
    assert await counter(db) == before


async def test_a_calculation_made_before_the_decision_still_issues(
    db: AsyncSession, hodim_client, paid_application: Application
):
    """The negative control for the check above: a decided application whose
    price predates the decision is the NORMAL case and must issue. Without this,
    "refuse whenever `decided_at` is set" would pass the test above just as
    well."""
    priced = await applications_service.current_calculation(db, paid_application.id)
    assert priced is not None
    paid_application.decided_at = priced.created_at + timedelta(minutes=5)
    await db.flush()

    result = await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")

    assert result.status_code == 201, result.text


async def test_a_hodim_from_another_leshoz_cannot_issue(
    other_zone_hodim_client, paid_application: Application
):
    """Zone scoping is not a permission check (lesson) — and `_assert_in_zone`
    reads all three axes of `Zone`, not `organization_id` alone."""
    result = await other_zone_hodim_client.post(f"{API}/applications/{paid_application.id}/permit")

    assert result.status_code == 403
    assert result.json()["error"]["code"] == "ERR-ACL-002"


async def test_an_applicant_cannot_issue_their_own_permit(
    applicant_client, paid_application: Application
):
    """The route's own dependency. A grantless `executor_staff` would NOT prove
    this: migration 0019 grants `permits.issue` to that role."""
    result = await applicant_client.post(f"{API}/applications/{paid_application.id}/permit")

    assert result.status_code == 403
    assert result.json()["error"]["code"] == "ERR-ACL-001"


async def test_issuance_writes_the_history_row_the_audit_entry_and_the_notification(
    db: AsyncSession, hodim_client, paid_application: Application, applicant_user: User
):
    """Steps 8, 10 and 11 of the order of operations, asserted together because
    they are one transaction: a permit whose timeline, trail or notification is
    missing is not an issued permit."""
    await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    permit = await service.for_application(db, paid_application.id)
    assert permit is not None

    history = (
        await db.scalars(
            select(PermitStatusHistory).where(PermitStatusHistory.permit_id == permit.id)
        )
    ).all()
    assert [(h.from_status, h.to_status) for h in history] == [(None, "pending_signatures")]

    trail = (
        await db.scalars(
            select(AuditLog).where(
                AuditLog.object_id == permit.id, AuditLog.action == service.PERMIT_ISSUE
            )
        )
    ).all()
    assert len(trail) == 1
    assert trail[0].result == "success"

    notified = (
        await db.scalars(
            select(Notification).where(
                Notification.object_id == permit.id,
                Notification.event_code == events.PERMIT_ISSUED,
            )
        )
    ).all()
    assert {n.recipient_user_id for n in notified} == {applicant_user.id}


async def test_the_series_letter_is_cyrillic_and_a_latin_a_hands_out_no_number(db: AsyncSession):
    """The counter is keyed on CYRILLIC А (U+0410). A Latin A (U+0041) looks
    identical, matches zero rows and makes `UPDATE ... RETURNING` return None —
    silently, unless the caller refuses to carry on without a number."""
    assert await repo.next_number(db, "А") is not None
    assert await repo.next_number(db, "A") is None


async def test_a_series_with_no_counter_row_fails_loudly_rather_than_silently(
    db: AsyncSession, hodim_client, paid_application: Application, monkeypatch: pytest.MonkeyPatch
):
    """The service arm of the trap above: configuration naming a series the
    database has no counter for must raise, not insert a permit with `number=None`
    (an IntegrityError 500 at flush) or, worse, number zero."""
    from app.config import get_settings

    monkeypatch.setenv("PERMIT_SERIES", "A")  # Latin A — the exact misconfiguration
    get_settings.cache_clear()

    result = await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")

    assert result.status_code == 500
    assert result.json()["error"]["code"] == "ERR-SYS-001"
    assert not (
        await db.scalars(select(Permit).where(Permit.application_id == paid_application.id))
    ).all()


async def test_a_missing_application_is_a_404(hodim_client):
    result = await hodim_client.post(f"{API}/applications/{uuid.uuid4()}/permit")

    assert result.status_code == 404
    assert result.json()["error"]["code"] == "ERR-SYS-003"


# --- the payment_confirmed subscriber (ruling 19) -----------------------------


async def test_payment_confirmed_tells_the_assigned_executor_and_issues_nothing(
    db: AsyncSession, paid_application: Application, assigned_executor: User
):
    """Ruling 19: this module subscribes ONLY to notify. A permit carrying a
    series number must not appear because a webhook fired — `design/03` makes
    issuance a human act."""
    await publish(
        db, Event(name=PAYMENT_CONFIRMED, payload={"application_id": paid_application.id})
    )

    notified = (
        await db.scalars(
            select(Notification).where(
                Notification.object_id == paid_application.id,
                Notification.event_code == events.PERMIT_DUE,
            )
        )
    ).all()
    assert {n.recipient_user_id for n in notified} == {assigned_executor.id}
    assert await service.for_application(db, paid_application.id) is None


async def test_payment_confirmed_with_nobody_assigned_notifies_nobody(
    db: AsyncSession, paid_application: Application
):
    """`assigned_user_id` is null until an executor picks the application up.
    There is then nobody to tell, and inventing a recipient would be worse than
    saying nothing."""
    assert paid_application.assigned_user_id is None

    await publish(
        db, Event(name=PAYMENT_CONFIRMED, payload={"application_id": paid_application.id})
    )

    notified = (
        await db.scalars(select(Notification).where(Notification.object_id == paid_application.id))
    ).all()
    assert notified == []


async def test_payment_confirmed_reads_the_amount_from_the_calculation_not_the_event(
    db: AsyncSession, paid_application: Application, assigned_executor: User
):
    """The event carries `application_id` and nothing else (applications/events.py).
    A figure on the event would be a SECOND source of truth for money."""
    calculation = (
        await db.scalars(
            select(Calculation).where(Calculation.application_id == paid_application.id)
        )
    ).one()

    await publish(
        db,
        Event(
            name=PAYMENT_CONFIRMED,
            payload={"application_id": paid_application.id, "amount": "1.00"},
        ),
    )

    notified = (
        await db.scalars(select(Notification).where(Notification.object_id == paid_application.id))
    ).all()
    assert notified
    assert all(n.params["amount"] == str(calculation.amount) for n in notified)


# --- form 1-ilova's remaining reachable requisites (ruling T3-c) ---------------


async def test_the_authority_and_the_leshoz_are_two_different_requisites(
    db: AsyncSession, hodim_client, paid_application: Application, leshoz: Organization
):
    """`tz/13` requisite 1 is «Название уполномоченного органа» and requisite 4 is
    «Ўрмон хўжалиги» — the agency that authorises the permit and the leshoz whose
    ground it covers. They are genuinely different rows here: `organizations.kind`
    is the chain `agency → territorial → leshoz → …` with a single-agency partial
    unique index, and the contour's own `organization_id` is the leshoz.

    One `organization_name` key used to serve both, with the agency's name hard-coded
    into the bundled layout's letterhead — so an agency rename would have left every
    permit printing the old name from a file nobody would think to look in."""
    agency = (
        await db.execute(select(Organization).where(Organization.kind == "agency"))
    ).scalar_one_or_none()
    assert agency is not None
    assert leshoz.parent_id == agency.id, "the fixture leshoz hangs off the single agency"

    await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    permit = await service.for_application(db, paid_application.id)
    assert permit is not None

    assert permit.snapshot["authority_name"] == agency.name["uz_cyrl"]
    assert permit.snapshot["leshoz_name"] == leshoz.name["uz_cyrl"]
    assert permit.snapshot["authority_name"] != permit.snapshot["leshoz_name"]


async def test_the_snapshot_carries_the_holders_address(
    db: AsyncSession, hodim_client, paid_application: Application, applicant_row: Applicant
):
    """`tz/13` requisite 11, «Адрес пользователя» — from the registry
    (`applicants.address`), frozen like every other requisite."""
    applicant_row.address = "Тошкент вилояти, Бўстонлиқ тумани, Бурчмулла қишлоғи, 12-уй"
    await db.flush()

    await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    permit = await service.for_application(db, paid_application.id)
    assert permit is not None

    assert permit.snapshot["holder_address"] == applicant_row.address


async def test_a_holder_with_no_address_on_record_is_still_issued_a_permit(
    db: AsyncSession, hodim_client, paid_application: Application, applicant_row: Applicant
):
    """Ruling T3-g. `applicants.address` is nullable by 3.2b's design —
    `CompleteRegistrationIn.address` is `str | None`, the citizen supplies it and many
    will not — so refusing here would strand somebody who has ALREADY PAID behind a
    profile edit only they can make, over a field that does not identify them
    (requisite 10, name plus PINFL/STIR, is the identity, and those are non-null).

    The form states «—» instead, the same way an empty head-count row does: a chosen
    value, not a blank. If the Agency wants the address mandatory the place for it is
    registration, where the rule would reach every future applicant."""
    applicant_row.address = None
    applicant_row.region_id = None
    applicant_row.district_id = None
    await db.flush()

    result = await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")

    assert result.status_code == 201, result.text
    permit = await service.for_application(db, paid_application.id)
    assert permit is not None
    assert permit.snapshot["holder_address"] == service.NOT_STATED


async def test_the_holders_address_falls_back_to_what_the_registry_does_hold(
    db: AsyncSession, hodim_client, paid_application: Application, applicant_row: Applicant
):
    """The middle case, and why `_holder_address` composes rather than reading one
    column: an applicant who chose a region and district at registration but typed no
    street still has a real, printable address. Widest first."""
    region = (await db.execute(select(Region).order_by(Region.sort_order))).scalars().first()
    assert region is not None
    # Built here, with a unique code: migration 0005 seeds the 14 regions but the ~208
    # districts arrive through the seed CLI, so a migrated test DB has none — and a
    # fixed literal would collide on `uq_districts_code` once the client's own commit
    # hook makes this row permanent (the shared-test-DB lesson).
    district = District(
        code=f"d-{uuid.uuid4().hex[:8]}",
        name={"uz_cyrl": "Синов тумани", "ru": "Тестовый район"},
        region_id=region.id,
    )
    db.add(district)
    await db.flush()

    applicant_row.address = None
    applicant_row.region_id = region.id
    applicant_row.district_id = district.id
    await db.flush()

    await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    permit = await service.for_application(db, paid_application.id)
    assert permit is not None

    assert permit.snapshot["holder_address"] == (
        f"{region.name['uz_cyrl']}, {district.name['uz_cyrl']}"
    )


async def test_the_printed_head_counts_come_from_the_frozen_calculation(
    db: AsyncSession, hodim_client, paid_application: Application, grazing_activity_id: uuid.UUID
):
    """Ruling T3-f. `tz/13` requisites 12-15 are read out of the `input_snapshot` of
    the calculation the permit is PRICED from, never out of `application_items` —
    which is a live table 3.9b's recalculation path may edit. The herd and the money
    are then frozen at the same moment and cannot disagree on a legal document.

    Proven by contradiction: an `application_items` row saying something else is
    planted before issuance, and the permit ignores it."""
    livestock_id = await db.scalar(
        text("SELECT id FROM livestock_types WHERE code = 'camel_adult'")
    )
    db.add(
        ApplicationItem(
            application_id=paid_application.id, livestock_type_id=livestock_id, head_count=999
        )
    )
    await db.flush()

    await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    permit = await service.for_application(db, paid_application.id)
    assert permit is not None

    # One row per tz/13 requisite, each naming its species from the classifier.
    assert permit.snapshot["heads_large_adult"] == "Қорамол (катта) — 5"
    assert permit.snapshot["heads_large_young"] == "От (2 ёшгача) — 2"
    assert permit.snapshot["heads_small_adult"] == "Қўй ва эчки (6 ойдан катта) — 2"
    assert permit.snapshot["heads_small_young"] == "Қўзи ва улоқ (6 ойгача) — 5"
    assert "999" not in "".join(
        str(permit.snapshot[key]) for key in permit.snapshot if key.startswith("heads_")
    ), "the live application_items row must not reach the document"


async def test_an_activity_that_commits_no_livestock_prints_no_head_counts(
    db: AsyncSession,
    hodim_client,
    apiary_paid_application: Application,
    apiary_template: PermitTemplate,
):
    """`CalcRequest.items` is empty for every activity but grazing — `quantity`
    carries those. The four head-count rows are then NOT APPLICABLE, and must say so
    rather than claim a herd of zero animals: an apiary permit reading «Қорамол — 0»
    would be a statement about cattle that nobody made."""
    await hodim_client.post(f"{API}/applications/{apiary_paid_application.id}/permit")
    permit = await service.for_application(db, apiary_paid_application.id)
    assert permit is not None

    heads = {key: permit.snapshot[key] for key in permit.snapshot if key.startswith("heads_")}
    assert set(heads) == {name for name, _ in service.LIVESTOCK_ROWS}, (
        "form 1-ilova has exactly these four head-count rows, by name"
    )
    assert set(heads.values()) == {service.NOT_STATED}
    # `"0" not in …` was implied by the line above and could never fail on its own.
    # The claim worth guarding is about NOT_STATED ITSELF: it is what all four rows
    # print, so a digit in it would turn an apiary permit into a statement about
    # cattle nobody made (final fix wave).
    assert not any(char.isdigit() for char in service.NOT_STATED), service.NOT_STATED


async def test_a_livestock_code_the_form_has_no_row_for_is_refused(
    db: AsyncSession,
    contours_layer,
    leshoz: Organization,
    approval_doc,
    grazing_activity_id: uuid.UUID,
    hodim_client,
):
    """`livestock_types` is an admin catalogue: an eleventh species can be added
    without anyone touching this module. Form 1-ilova has exactly four head-count
    rows, so a code belonging to none of them cannot be printed — and silently
    DROPPING it would understate the herd on a legal permit while the fee, computed
    from the same list, still charged for it. Loud, naming the code."""
    application = await make_paid_application(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
        items=(("cattle_adult", 3), ("yak_adult", 1)),
    )
    before = await counter(db)

    result = await hodim_client.post(f"{API}/applications/{application.id}/permit")

    assert result.status_code == 422
    assert result.json()["error"]["code"] == "ERR-VAL-001"
    details = result.json()["error"]["details"]
    assert details["reason"] == "unknown_livestock_code"
    assert details["code"] == "yak_adult"
    # The snapshot is assembled AFTER `next_number` (it needs the number), so unlike
    # the no-calculation refusal above this one does take a number — and gives it
    # back, because the raise rolls the whole transaction out. Asserted, not assumed:
    # a gap in a legal register is unexplainable years later either way.
    assert await counter(db) == before


async def test_every_snapshot_key_is_printed_by_the_bundled_layout(
    db: AsyncSession, hodim_client, paid_application: Application
):
    """A key nobody prints is dead weight in an immutable record, and a placeholder
    with no key is `ERR-VAL-001` at the first real issuance. The bundled layout and
    `service._snapshot` therefore have to be kept in step BOTH ways — mechanically,
    because `tz/13` is a 25-row table and drift here is invisible until a permit is
    refused or a requisite quietly stops being printed.

    `calculation_id` is the one documented exception: it is the link back to the
    invoice that was actually paid (ruling 19), deliberately not on the form."""
    await hodim_client.post(f"{API}/applications/{paid_application.id}/permit")
    permit = await service.for_application(db, paid_application.id)
    assert permit is not None

    printed = {m.group(1).strip() for m in render._PLACEHOLDER.finditer(render.default_layout())}
    printed.discard(render.QR_FIELD)  # the renderer fills it, not the snapshot

    assert printed == set(permit.snapshot) - {"calculation_id"}
