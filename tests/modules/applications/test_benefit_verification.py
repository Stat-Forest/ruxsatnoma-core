"""A benefit claim's verify/reject pair, moved to the leshoz by ruling #182
(wave-2 track B2; `docs/plans/10-benefits-and-simple-signature.md`).

Rulings #179/#181/#182. Every positive test below is built through the REAL
routes end to end — `POST /applications` -> `PATCH` -> a document of the
benefit type -> `/submit` -> `/start-review` -> `/verify`|`/reject` — never by
stamping `benefit_verification_status` on the row directly (lesson: build a
fixture's precondition through the real transition). `benefit_categories` is
no longer empty (ruling #181, migration `0053`), so this file uses the REAL
seven seeded categories. Ruling #219 (2026-09-24) puts back the automatic
register check #206 had switched off: a `beekeeping_union_member` claim is
checked against the Beekeeping Union's register at the pre-check, at the
package and at filing — refused when the number is unknown, someone else's
or expired, VERIFIED on the spot when it matches. Every other category still
opens `pending` for the leshoz. The last section of this file pins both.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Classifier, ClassifierItem
from app.modules.applications import service
from app.modules.applications.benefit_verification import (
    BENEFIT_CLAIM_REJECT,
    BENEFIT_CLAIM_VERIFY,
)
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Applicant, User
from app.modules.beekeepers import service as beekeepers_service
from app.modules.beekeepers.schemas import BeekeeperCreateIn
from app.modules.norms.models import Tariff
from tests.modules.applications.conftest import unique_pinfl
from tests.modules.applications.test_submit import _submit, _submit_with_button, _upload

API = "/api/v1"


async def _benefit_category_item_id(db: AsyncSession, code: str) -> uuid.UUID:
    """One ACTIVE `benefit_categories` item by its CODE — the seven ruling
    #181 seeds via migration `0053`, fetched rather than duplicated: a
    private copy inserted per test would leave rows in this shared,
    persistent database that `GET /refs/classifiers/benefit_categories`
    would then offer on a real form."""
    classifier_id = (
        await db.execute(select(Classifier.id).where(Classifier.code == "benefit_categories"))
    ).scalar_one()
    return (
        await db.execute(
            select(ClassifierItem.id).where(
                ClassifierItem.classifier_id == classifier_id,
                ClassifierItem.code == code,
                ClassifierItem.status == "active",
            )
        )
    ).scalar_one()


async def _claim_and_prove(
    applicant_client,
    filing: dict[str, Any],
    *,
    benefit_category_item_id: uuid.UUID,
    benefit_doc_type_item_id: uuid.UUID,
    certificate_no: str | None,
) -> dict[str, Any]:
    """The filing with the claim on it and the one document type that proves
    it attached — stage 12's one-request shape (plan 12, R1/R9): the scan is
    uploaded first, the filing carries its id."""
    body: dict[str, Any] = {
        **filing,
        "benefit_category_item_id": str(benefit_category_item_id),
        "documents": [
            {
                "doc_type_item_id": str(benefit_doc_type_item_id),
                "file_id": await _upload(applicant_client),
            }
        ],
    }
    if certificate_no is not None:
        body["benefit_certificate_no"] = certificate_no
    return body


@pytest.fixture
async def pending_claim_application(
    applicant_client,
    hodim_client,
    recreation_filing_ready_for_submission: dict[str, Any],
    preschool_children_item_id: uuid.UUID,
    preschool_children_priced: None,
    benefit_doc_type_item_id: uuid.UUID,
) -> str:
    """A genuinely SUBMITTED-then-`IN_REVIEW` application carrying a `pending`
    claim on a category with NO registered auto-verifier — the row every
    positive verify/reject test below acts on.

    **A REAL category (`preschool_children`), not an invented one.** Since
    #181, an invented code can never reach SUBMITTED at all: decision #50
    refuses any benefit code absent from the resolved tariff's `benefit_
    modifiers` at PRICING time, inside the same transaction step 3b runs in
    — so a fixture built on a made-up code rolls back before `pending` is
    ever committed. `preschool_children_priced` gives the seeded recreation
    tariff a real (non-zero, non-self-settling) modifier for exactly this
    code.
    """
    filing = await _claim_and_prove(
        applicant_client,
        recreation_filing_ready_for_submission,
        benefit_category_item_id=preschool_children_item_id,
        benefit_doc_type_item_id=benefit_doc_type_item_id,
        certificate_no="CERT-0001",
    )
    submitted = await _submit(applicant_client, filing)
    assert submitted.status_code == 201, submitted.text
    app_id = submitted.json()["id"]
    assert submitted.json()["benefit_verification_status"] == "pending"

    started = await hodim_client.post(f"{API}/applications/{app_id}/start-review")
    assert started.status_code == 200, started.text
    return app_id


@pytest.fixture
async def submitted_claim_not_yet_in_review(
    applicant_client,
    recreation_filing_ready_for_submission: dict[str, Any],
    preschool_children_item_id: uuid.UUID,
    preschool_children_priced: None,
    benefit_doc_type_item_id: uuid.UUID,
) -> str:
    """The SAME shape as `pending_claim_application`, stopped one step short —
    `start-review` never runs, so the application is `SUBMITTED`, not
    `IN_REVIEW`. The one row `test_verify_before_review_started_is_refused`
    needs."""
    filing = await _claim_and_prove(
        applicant_client,
        recreation_filing_ready_for_submission,
        benefit_category_item_id=preschool_children_item_id,
        benefit_doc_type_item_id=benefit_doc_type_item_id,
        certificate_no="CERT-0002",
    )
    submitted = await _submit(applicant_client, filing)
    assert submitted.status_code == 201, submitted.text
    app_id = submitted.json()["id"]
    return app_id


# --- Visibility: the leshoz's own read/zone rule, not a central one ----------


async def test_a_reviewer_reading_an_unrelated_application_gets_the_same_404_as_a_stranger(
    hodim_client, submitted_application
) -> None:
    """`submitted_application` (conftest's own, no benefit claim at all) is a
    real, submitted, otherwise-fully-readable-by-`hodim_client` row in the
    SAME leshoz — and this route still answers exactly what a stranger to the
    whole system gets, because it carries no certificate-bearing claim."""
    unrelated = await hodim_client.get(
        f"{API}/applications/benefit-verifications/{submitted_application}"
    )
    assert unrelated.status_code == 404
    assert unrelated.json()["error"]["code"] == "ERR-SYS-003"

    nonexistent = await hodim_client.get(f"{API}/applications/benefit-verifications/{uuid.uuid4()}")
    assert nonexistent.status_code == 404
    assert nonexistent.json()["error"]["code"] == "ERR-SYS-003"
    assert unrelated.json()["error"]["message"] == nonexistent.json()["error"]["message"]


async def test_an_executor_of_another_leshoz_gets_404(
    other_zone_hodim_client, pending_claim_application
) -> None:
    """Ruling #182's whole point: this is the leshoz's OWN review now, not a
    country-wide one — a reviewer zoned to a DIFFERENT leshoz gets the same
    404 `GET /applications/{id}` would give them."""
    read = await other_zone_hodim_client.get(
        f"{API}/applications/benefit-verifications/{pending_claim_application}"
    )
    assert read.status_code == 404
    assert read.json()["error"]["code"] == "ERR-SYS-003"

    verify = await other_zone_hodim_client.post(
        f"{API}/applications/benefit-verifications/{pending_claim_application}/verify"
    )
    assert verify.status_code == 404
    assert verify.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_gis_specialist_is_refused_the_whole_surface(
    gis_specialist_client, pending_claim_application
) -> None:
    """A real staff role of the SAME leshoz that does not hold
    `benefits.verify` — `gis_specialist` holds neither `.review` nor
    `benefits.verify` since migration `0053` (ruling #182)."""
    result = await gis_specialist_client.get(
        f"{API}/applications/benefit-verifications/{pending_claim_application}"
    )
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "ERR-ACL-001"


async def test_an_applicant_is_refused_the_whole_surface(
    applicant_client, pending_claim_application
) -> None:
    result = await applicant_client.get(
        f"{API}/applications/benefit-verifications/{pending_claim_application}"
    )
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "ERR-ACL-001"


# --- The detail read -----------------------------------------------------------


async def test_the_detail_read_carries_the_certificate_and_its_documents(
    hodim_client, pending_claim_application
) -> None:
    result = await hodim_client.get(
        f"{API}/applications/benefit-verifications/{pending_claim_application}"
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["id"] == pending_claim_application
    assert body["benefit_certificate_no"] == "CERT-0001"
    assert body["benefit_verification_status"] == "pending"
    assert body["benefit_verified_by"] is None
    assert body["benefit_verified_at"] is None
    assert len(body["documents"]) == 1


# --- verify --------------------------------------------------------------------


async def test_an_executor_in_zone_verifies_and_it_is_audited(
    db: AsyncSession, hodim_client, pending_claim_application
) -> None:
    result = await hodim_client.post(
        f"{API}/applications/benefit-verifications/{pending_claim_application}/verify"
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["benefit_verification_status"] == "verified"
    assert body["benefit_verified_by"] is not None
    assert body["benefit_verified_at"] is not None
    assert body["benefit_rejection_reason"] is None

    entry = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == BENEFIT_CLAIM_VERIFY,
                AuditLog.object_id == uuid.UUID(pending_claim_application),
            )
        )
    ).scalar_one()
    assert entry.result == "success"
    assert entry.new_value is not None
    assert entry.new_value["benefit_verification_status"] == "verified"


async def test_verify_before_review_started_is_refused(
    hodim_client, submitted_claim_not_yet_in_review
) -> None:
    """Ruling #182: verify/reject require the application to be `IN_REVIEW` —
    "the moderator checks it when the application comes in", not while it is
    still sitting `SUBMITTED`."""
    result = await hodim_client.post(
        f"{API}/applications/benefit-verifications/{submitted_claim_not_yet_in_review}/verify"
    )
    assert result.status_code == 409, result.text
    error = result.json()["error"]
    assert error["code"] == "ERR-APP-004"
    assert error["details"]["reason"] == "not_in_review"
    assert error["details"]["status"] == "SUBMITTED"


async def test_verifying_an_already_decided_claim_is_refused(
    hodim_client, pending_claim_application
) -> None:
    first = await hodim_client.post(
        f"{API}/applications/benefit-verifications/{pending_claim_application}/verify"
    )
    assert first.status_code == 200, first.text

    second = await hodim_client.post(
        f"{API}/applications/benefit-verifications/{pending_claim_application}/verify"
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ERR-APP-004"
    assert second.json()["error"]["details"]["reason"] == "not_pending"


# --- reject ----------------------------------------------------------------


async def test_reject_requires_a_reason(hodim_client, pending_claim_application) -> None:
    missing = await hodim_client.post(
        f"{API}/applications/benefit-verifications/{pending_claim_application}/reject", json={}
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "ERR-VAL-001"

    blank = await hodim_client.post(
        f"{API}/applications/benefit-verifications/{pending_claim_application}/reject",
        json={"reason": ""},
    )
    assert blank.status_code == 422
    assert blank.json()["error"]["code"] == "ERR-VAL-001"


async def test_reject_moves_pending_to_rejected_with_the_reason_and_is_audited(
    db: AsyncSession, hodim_client, pending_claim_application
) -> None:
    reason = "the certificate's serial does not match the registry"
    result = await hodim_client.post(
        f"{API}/applications/benefit-verifications/{pending_claim_application}/reject",
        json={"reason": reason},
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["benefit_verification_status"] == "rejected"
    assert body["benefit_rejection_reason"] == reason
    assert body["benefit_verified_by"] is not None
    assert body["benefit_verified_at"] is not None

    entry = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == BENEFIT_CLAIM_REJECT,
                AuditLog.object_id == uuid.UUID(pending_claim_application),
            )
        )
    ).scalar_one()
    assert entry.result == "success"
    assert entry.new_value is not None
    assert entry.new_value["benefit_verification_status"] == "rejected"
    assert entry.new_value["reason"] == reason


# --- ruling #181: a number is mandatory for EVERY category, flagged or not --


async def test_a_claim_without_a_number_is_refused_at_submission_on_any_category(
    applicant_client,
    filing_ready_for_submission,
    benefit_category_item_id: uuid.UUID,
    benefit_doc_type_item_id: uuid.UUID,
) -> None:
    """Ruling #181's own change: `benefit_category_item_id` here is an
    ordinary, unflagged category (conftest's fixture carries no
    `props.requires_certificate` at all any more, because that property is no
    longer read) — and a claim naming it with no certificate number is STILL
    refused, exactly like a "flagged" one used to be. `requires_certificate`
    is gone from the code, not merely false for this row."""
    filing = await _claim_and_prove(
        applicant_client,
        filing_ready_for_submission,
        benefit_category_item_id=benefit_category_item_id,
        benefit_doc_type_item_id=benefit_doc_type_item_id,
        certificate_no=None,
    )

    result = await _submit_with_button(applicant_client, filing)
    assert result.status_code == 422, result.text
    error = result.json()["error"]
    assert error["code"] == "ERR-APP-003"
    assert error["details"]["reason"] == "benefit_certificate_required"


async def test_a_claim_with_its_number_on_an_unflagged_category_opens_pending_verification(
    applicant_client,
    filing_ready_for_submission,
    benefit_category_item_id: uuid.UUID,
    benefit_doc_type_item_id: uuid.UUID,
) -> None:
    """The number alone is enough to pass step 3b on a category with no
    registered auto-verifier — it still ends 422 downstream, for the reason
    `test_submit.py::test_a_benefit_claim_is_accepted_without_a_document`
    documents (no seeded tariff carries a modifier for a code a test
    invented); what this pins is WHICH gate answers first."""
    filing = await _claim_and_prove(
        applicant_client,
        filing_ready_for_submission,
        benefit_category_item_id=benefit_category_item_id,
        benefit_doc_type_item_id=benefit_doc_type_item_id,
        certificate_no="CERT-7788",
    )

    result = await _submit_with_button(applicant_client, filing)
    assert result.status_code == 422, result.text
    body = result.json()
    assert body["error"]["code"] != "ERR-APP-003"


# --- ruling #219: the Union's register, at the pre-check and at filing -------
#
# #219 (2026-09-24) restores the register half of #182 that #206 switched off,
# and brings it FORWARD to the pre-check the wizard runs on leaving step 4 —
# the Agency's own text asks for the check on the "Next" button. The refusal
# tests pre-check or file a GRAZING filing: the register answers before
# pricing does, so a code the grazing tariff would never price still shows
# WHICH gate answered first. The tests that must get PAST the register file a
# real APIARY filing whose seeded tariff is given a modifier for the one
# register-backed code (`beekeeping_priced`).

BEEKEEPING = "beekeeping_union_member"


@pytest.fixture
async def apiary_activity_id(db: AsyncSession) -> uuid.UUID:
    """`activity_types.code = 'apiary'` — the one activity the Union member's
    category is scoped to (migration `0053`, `props.activity`)."""
    return (
        await db.execute(text("SELECT id FROM activity_types WHERE code = 'apiary'"))
    ).scalar_one()


@pytest.fixture
async def beekeeping_priced(db: AsyncSession, apiary_activity_id: uuid.UUID) -> AsyncIterator[None]:
    """`conftest.py::preschool_children_priced`'s shape for the seeded,
    published `apiary` tariff (migration `0012`): a non-zero modifier for the
    Union member's code, so pricing accepts it (decision #50) without also
    self-settling (#185). Flips a SHARED singleton seed row and restores it in
    `finally`, exactly as that fixture does."""
    tariff = (
        await db.execute(
            select(Tariff).where(
                Tariff.activity_type_id == apiary_activity_id,
                Tariff.livestock_group.is_(None),
                Tariff.status == "published",
            )
        )
    ).scalar_one()
    original = tariff.benefit_modifiers
    tariff.benefit_modifiers = {BEEKEEPING: "0.5"}
    await db.commit()
    try:
        yield
    finally:
        tariff.benefit_modifiers = original
        await db.commit()


@pytest.fixture
def apiary_filing(
    applicant: Applicant, published_contour, apiary_activity_id: uuid.UUID
) -> dict[str, Any]:
    """A complete APIARY filing — priced by `quantity` (hives), no herd.
    `applicant` is a dependency so the caller's own `applicants` row, with its
    address, exists."""
    return {
        "on_behalf": "self",
        "contour_id": str(published_contour.id),
        "activity_type_id": str(apiary_activity_id),
        "period_from": "2027-05-01",
        "period_to": "2027-08-31",
        "quantity": "10",
    }


async def _register_beekeeper(
    db: AsyncSession,
    actor: User,
    *,
    pinfl: str,
    stir: str | None = None,
    valid_to: date | None = None,
) -> str:
    """One REAL, ACTIVE row of the Union's register through `beekeepers.
    service.create_beekeeper` — never a stub. Returns the number in lower
    case: the register folds it on write and on read, so the filing below
    also proves the claim does not depend on how the citizen typed it."""
    certificate_no = f"bee-{uuid.uuid4().hex[:10]}"
    await beekeepers_service.create_beekeeper(
        db,
        data=BeekeeperCreateIn(
            certificate_no=certificate_no,
            pinfl=pinfl,
            passport_series="AB",
            passport_number="1234567",
            stir=stir,
            full_name="Registered Beekeeper",
            valid_to=valid_to,
        ),
        actor=actor,
    )
    await db.commit()
    return certificate_no


async def _beekeeping_claim(
    db: AsyncSession, filing: dict[str, Any], certificate_no: str
) -> dict[str, Any]:
    return {
        **filing,
        "benefit_category_item_id": str(await _benefit_category_item_id(db, BEEKEEPING)),
        "benefit_certificate_no": certificate_no,
    }


def _assert_register_refusal(result, reason: str) -> None:
    assert result.status_code == 422, result.text
    error = result.json()["error"]
    assert error["code"] == "ERR-APP-003"
    assert error["details"]["reason"] == reason


async def test_the_precheck_refuses_a_number_the_register_does_not_know(
    db: AsyncSession, applicant_client, filing_ready_for_submission
) -> None:
    filing = await _beekeeping_claim(
        db, filing_ready_for_submission, f"BEE-{uuid.uuid4().hex[:10]}"
    )

    result = await applicant_client.post(f"{API}/applications/precheck", json=filing)

    _assert_register_refusal(result, "benefit_certificate_unknown")


async def test_the_precheck_refuses_someone_elses_number(
    db: AsyncSession, applicant_client, filing_ready_for_submission, hodim_user: User
) -> None:
    certificate_no = await _register_beekeeper(db, hodim_user, pinfl=unique_pinfl())
    filing = await _beekeeping_claim(db, filing_ready_for_submission, certificate_no)

    result = await applicant_client.post(f"{API}/applications/precheck", json=filing)

    _assert_register_refusal(result, "benefit_certificate_not_yours")


async def test_the_precheck_refuses_an_expired_certificate(
    db: AsyncSession,
    applicant: Applicant,
    applicant_client,
    filing_ready_for_submission,
    hodim_user: User,
) -> None:
    """Ruling #217's term: a certificate that lapsed long before any business
    day this suite can run on — a fixed past date, never one computed from
    the clock (lesson)."""
    assert applicant.pinfl is not None
    certificate_no = await _register_beekeeper(
        db, hodim_user, pinfl=applicant.pinfl, valid_to=date(2020, 12, 31)
    )
    filing = await _beekeeping_claim(db, filing_ready_for_submission, certificate_no)

    result = await applicant_client.post(f"{API}/applications/precheck", json=filing)

    _assert_register_refusal(result, "benefit_certificate_expired")


async def test_the_precheck_passes_the_applicants_own_number_and_prices_it(
    db: AsyncSession,
    applicant: Applicant,
    applicant_client,
    apiary_filing: dict[str, Any],
    beekeeping_priced: None,
    hodim_user: User,
) -> None:
    assert applicant.pinfl is not None
    certificate_no = await _register_beekeeper(db, hodim_user, pinfl=applicant.pinfl)
    filing = await _beekeeping_claim(db, apiary_filing, certificate_no)

    result = await applicant_client.post(f"{API}/applications/precheck", json=filing)

    assert result.status_code == 200, result.text
    assert result.json()["calculation"] is not None


async def test_a_legal_entitys_claim_is_matched_by_its_stir_not_the_representatives_pinfl(
    db: AsyncSession,
    legal_applicant: Applicant,
    representative_client,
    apiary_filing: dict[str, Any],
    beekeeping_priced: None,
    hodim_user: User,
) -> None:
    """A farm files through its representative: the register row names the
    ENTITY's STIR, and a stranger's PINFL beside it — the claim is the
    entity's, so it matches."""
    assert legal_applicant.stir is not None
    certificate_no = await _register_beekeeper(
        db, hodim_user, pinfl=unique_pinfl(), stir=legal_applicant.stir
    )
    filing = await _beekeeping_claim(
        db,
        {**apiary_filing, "on_behalf": "legal", "applicant_id": str(legal_applicant.id)},
        certificate_no,
    )

    result = await representative_client.post(f"{API}/applications/precheck", json=filing)

    assert result.status_code == 200, result.text


async def test_the_package_refuses_a_number_the_register_does_not_know(
    db: AsyncSession, applicant_client, filing_ready_for_submission
) -> None:
    """The ERI path fetches the package BEFORE the signature: a claim the
    register refuses must never reach the E-IMZO dialog at all."""
    filing = await _beekeeping_claim(
        db, filing_ready_for_submission, f"BEE-{uuid.uuid4().hex[:10]}"
    )

    result = await applicant_client.post(f"{API}/applications/package", json=filing)

    _assert_register_refusal(result, "benefit_certificate_unknown")


async def test_filing_refuses_a_number_the_register_does_not_know(
    db: AsyncSession, applicant_client, filing_ready_for_submission
) -> None:
    """The filing re-checks on its own: the register may have changed since
    the pre-check, and a client may skip the pre-check altogether."""
    filing = await _beekeeping_claim(
        db, filing_ready_for_submission, f"BEE-{uuid.uuid4().hex[:10]}"
    )

    result = await _submit_with_button(applicant_client, filing)

    _assert_register_refusal(result, "benefit_certificate_unknown")


async def test_a_matching_number_is_verified_by_the_register_at_filing(
    db: AsyncSession,
    applicant: Applicant,
    applicant_client,
    apiary_filing: dict[str, Any],
    beekeeping_priced: None,
    hodim_user: User,
) -> None:
    """End to end: the claim reaches SUBMITTED already `verified`, with
    `benefit_verified_by` NULL — ruling #182's own meaning, "the register,
    not a human" — so the leshoz has nothing left to decide about it."""
    assert applicant.pinfl is not None
    certificate_no = await _register_beekeeper(db, hodim_user, pinfl=applicant.pinfl)
    filing = await _beekeeping_claim(db, apiary_filing, certificate_no)

    submitted = await _submit_with_button(applicant_client, filing)

    assert submitted.status_code == 201, submitted.text
    body = submitted.json()
    assert body["benefit_verification_status"] == "verified"
    assert body["benefit_verified_by"] is None
    assert body["benefit_verified_at"] is not None


async def test_a_register_match_replaces_a_previous_attempts_verdict(
    db: AsyncSession,
    applicant: Applicant,
    submitted_application: str,
    hodim_user: User,
) -> None:
    """In-process, over the step `submit` (a RETURNED row's resubmission) and
    `file` both call: a match verifies on the spot and wipes whatever a
    previous attempt was told."""
    assert applicant.pinfl is not None
    certificate_no = await _register_beekeeper(db, hodim_user, pinfl=applicant.pinfl)
    application = await service.get(db, uuid.UUID(submitted_application))
    assert application is not None
    application.benefit_category_item_id = await _benefit_category_item_id(db, BEEKEEPING)
    application.benefit_certificate_no = certificate_no
    application.benefit_verification_status = "rejected"
    application.benefit_verified_by = hodim_user.id
    application.benefit_verified_at = datetime(2026, 1, 1, tzinfo=UTC)
    application.benefit_rejection_reason = "previous attempt"
    await db.flush()

    await service._open_benefit_verification(db, application)

    assert application.benefit_verification_status == "verified"
    assert application.benefit_verified_by is None, "ruling #182: NULL means the register"
    assert application.benefit_verified_at is not None
    assert application.benefit_verified_at != datetime(2026, 1, 1, tzinfo=UTC)
    assert application.benefit_rejection_reason is None


async def test_a_category_with_no_register_still_opens_pending(
    applicant_client,
    recreation_filing_ready_for_submission: dict[str, Any],
    preschool_children_item_id: uuid.UUID,
    preschool_children_priced: None,
    benefit_doc_type_item_id: uuid.UUID,
) -> None:
    """The negative control of this section: only the Union keeps a
    register. The six recreation categories are the leshoz's own decision, so
    any number passes the pre-check and the filing opens `pending`."""
    filing = await _claim_and_prove(
        applicant_client,
        recreation_filing_ready_for_submission,
        benefit_category_item_id=preschool_children_item_id,
        benefit_doc_type_item_id=benefit_doc_type_item_id,
        certificate_no="CERT-9001",
    )
    prechecked = await applicant_client.post(f"{API}/applications/precheck", json=filing)
    assert prechecked.status_code == 200, prechecked.text

    submitted = await _submit(applicant_client, filing)
    assert submitted.status_code == 201, submitted.text
    assert submitted.json()["benefit_verification_status"] == "pending"
