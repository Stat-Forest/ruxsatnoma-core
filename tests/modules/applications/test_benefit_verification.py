"""A benefit claim's verify/reject pair, moved to the leshoz by ruling #182
(wave-2 track B2; `docs/plans/10-benefits-and-simple-signature.md`).

Rulings #179/#181/#182. Every positive test below is built through the REAL
routes end to end — `POST /applications` -> `PATCH` -> a document of the
benefit type -> `/submit` -> `/start-review` -> `/verify`|`/reject` — never by
stamping `benefit_verification_status` on the row directly (lesson: build a
fixture's precondition through the real transition). `benefit_categories` is
no longer empty (ruling #181, migration `0053`), so this file uses the REAL
seven seeded categories — `beekeeping_union_member` for the wired seam,
`conftest.py`'s own `benefit_category_item_id` (a fresh, unrelated code with
no registered auto-verifier) for the pending/leshoz-review path.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Classifier, ClassifierItem
from app.modules.applications import service
from app.modules.applications.benefit_verification import (
    BENEFIT_CLAIM_REJECT,
    BENEFIT_CLAIM_VERIFY,
)
from app.modules.audit.models import AuditLog
from app.modules.auth.models import User
from app.modules.beekeepers import service as beekeepers_service
from app.modules.beekeepers.schemas import BeekeeperCreateIn
from tests.modules.applications.conftest import unique_pinfl
from tests.modules.applications.test_submit import _submit, _upload

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
    app_id: str,
    *,
    benefit_category_item_id: uuid.UUID,
    benefit_doc_type_item_id: uuid.UUID,
    certificate_no: str | None,
):
    """PATCH the claim onto a draft and attach the one document type that
    proves it — `test_submit.py`'s own two-step shape, factored out because
    every test below needs it before it can even attempt `/submit`."""
    body: dict[str, object] = {"benefit_category_item_id": str(benefit_category_item_id)}
    if certificate_no is not None:
        body["benefit_certificate_no"] = certificate_no
    patched = await applicant_client.patch(f"{API}/applications/{app_id}", json=body)
    assert patched.status_code == 200, patched.text
    proof = await applicant_client.post(
        f"{API}/applications/{app_id}/documents",
        json={
            "doc_type_item_id": str(benefit_doc_type_item_id),
            "file_id": await _upload(applicant_client),
        },
    )
    assert proof.status_code == 201, proof.text


@pytest.fixture
async def pending_claim_application(
    applicant_client,
    hodim_client,
    recreation_draft_ready_for_submission: str,
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
    app_id = recreation_draft_ready_for_submission
    await _claim_and_prove(
        applicant_client,
        app_id,
        benefit_category_item_id=preschool_children_item_id,
        benefit_doc_type_item_id=benefit_doc_type_item_id,
        certificate_no="CERT-0001",
    )
    submitted = await _submit(applicant_client, app_id)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["benefit_verification_status"] == "pending"

    started = await hodim_client.post(f"{API}/applications/{app_id}/start-review")
    assert started.status_code == 200, started.text
    return app_id


@pytest.fixture
async def submitted_claim_not_yet_in_review(
    applicant_client,
    recreation_draft_ready_for_submission: str,
    preschool_children_item_id: uuid.UUID,
    preschool_children_priced: None,
    benefit_doc_type_item_id: uuid.UUID,
) -> str:
    """The SAME shape as `pending_claim_application`, stopped one step short —
    `start-review` never runs, so the application is `SUBMITTED`, not
    `IN_REVIEW`. The one row `test_verify_before_review_started_is_refused`
    needs."""
    app_id = recreation_draft_ready_for_submission
    await _claim_and_prove(
        applicant_client,
        app_id,
        benefit_category_item_id=preschool_children_item_id,
        benefit_doc_type_item_id=benefit_doc_type_item_id,
        certificate_no="CERT-0002",
    )
    submitted = await _submit(applicant_client, app_id)
    assert submitted.status_code == 200, submitted.text
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
    draft_ready_for_submission,
    benefit_category_item_id: uuid.UUID,
    benefit_doc_type_item_id: uuid.UUID,
) -> None:
    """Ruling #181's own change: `benefit_category_item_id` here is an
    ordinary, unflagged category (conftest's fixture carries no
    `props.requires_certificate` at all any more, because that property is no
    longer read) — and a claim naming it with no certificate number is STILL
    refused, exactly like a "flagged" one used to be. `requires_certificate`
    is gone from the code, not merely false for this row."""
    app_id = draft_ready_for_submission
    await _claim_and_prove(
        applicant_client,
        app_id,
        benefit_category_item_id=benefit_category_item_id,
        benefit_doc_type_item_id=benefit_doc_type_item_id,
        certificate_no=None,
    )

    result = await _submit(applicant_client, app_id)
    assert result.status_code == 422, result.text
    error = result.json()["error"]
    assert error["code"] == "ERR-APP-003"
    assert error["details"]["reason"] == "benefit_certificate_required"


async def test_a_claim_with_its_number_on_an_unflagged_category_opens_pending_verification(
    applicant_client,
    draft_ready_for_submission,
    benefit_category_item_id: uuid.UUID,
    benefit_doc_type_item_id: uuid.UUID,
) -> None:
    """The number alone is enough to pass step 3b on a category with no
    registered auto-verifier — it still ends 422 downstream, for the reason
    `test_submit.py::test_a_benefit_claim_is_accepted_without_a_document`
    documents (no seeded tariff carries a modifier for a code a test
    invented); what this pins is WHICH gate answers first."""
    app_id = draft_ready_for_submission
    await _claim_and_prove(
        applicant_client,
        app_id,
        benefit_category_item_id=benefit_category_item_id,
        benefit_doc_type_item_id=benefit_doc_type_item_id,
        certificate_no="CERT-7788",
    )

    result = await _submit(applicant_client, app_id)
    assert result.status_code == 422, result.text
    body = result.json()
    assert body["error"]["code"] != "ERR-APP-003"


# --- ruling #182: the wired seam, real register, three outcomes ---------------


async def test_the_wired_seam_empty_for_every_other_category(
    applicant_client,
    recreation_draft_ready_for_submission: str,
    preschool_children_item_id: uuid.UUID,
    preschool_children_priced: None,
    benefit_doc_type_item_id: uuid.UUID,
) -> None:
    """`BENEFIT_AUTO_VERIFIERS` holds exactly one entry (`beekeeping_union_
    member`); every other REAL #181 category — `preschool_children` here —
    genuinely reaches SUBMITTED and falls through to the leshoz's own
    `pending` queue, restated as this file's own negative control against
    the wired seam specifically: a category that is not `beekeeping_union_
    member` must never be auto-verified, whatever it prices to."""
    app_id = recreation_draft_ready_for_submission
    await _claim_and_prove(
        applicant_client,
        app_id,
        benefit_category_item_id=preschool_children_item_id,
        benefit_doc_type_item_id=benefit_doc_type_item_id,
        certificate_no="CERT-9001",
    )
    submitted = await _submit(applicant_client, app_id)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["benefit_verification_status"] == "pending"


async def test_the_wired_seam_refuses_an_unregistered_certificate_number(
    db: AsyncSession,
    applicant_client,
    draft_ready_for_submission,
    benefit_doc_type_item_id: uuid.UUID,
) -> None:
    """The REAL `beekeeping_union_member` category (migration `0053`) and the
    REAL `beekeepers.service.match_certificate` (wired from `app/
    event_subscriptions.py`, active in every test process — `register_event_
    subscriptions()` runs autouse) against a certificate number nobody has
    ever registered: `unknown`."""
    item_id = await _benefit_category_item_id(db, "beekeeping_union_member")
    app_id = draft_ready_for_submission
    await _claim_and_prove(
        applicant_client,
        app_id,
        benefit_category_item_id=item_id,
        benefit_doc_type_item_id=benefit_doc_type_item_id,
        certificate_no=f"BEE-{uuid.uuid4().hex[:10]}",
    )

    result = await _submit(applicant_client, app_id)
    assert result.status_code == 422, result.text
    error = result.json()["error"]
    assert error["code"] == "ERR-APP-003"
    assert error["details"]["reason"] == "benefit_certificate_unknown"


async def test_the_wired_seam_refuses_someone_elses_certificate_number(
    db: AsyncSession,
    applicant_client,
    draft_ready_for_submission,
    benefit_doc_type_item_id: uuid.UUID,
    hodim_user: User,
) -> None:
    """A REAL, ACTIVE `beekeepers` row — seeded through `beekeepers.service.
    create_beekeeper`, never a stub — under a PINFL that is NOT the
    applicant's own: `not_yours`."""
    item_id = await _benefit_category_item_id(db, "beekeeping_union_member")
    certificate_no = f"BEE-{uuid.uuid4().hex[:10]}"
    await beekeepers_service.create_beekeeper(
        db,
        data=BeekeeperCreateIn(
            certificate_no=certificate_no,
            pinfl=unique_pinfl(),
            passport_series="AB",
            passport_number="1234567",
            full_name="Someone Else",
        ),
        actor=hodim_user,
    )
    await db.commit()

    app_id = draft_ready_for_submission
    await _claim_and_prove(
        applicant_client,
        app_id,
        benefit_category_item_id=item_id,
        benefit_doc_type_item_id=benefit_doc_type_item_id,
        certificate_no=certificate_no,
    )

    result = await _submit(applicant_client, app_id)
    assert result.status_code == 422, result.text
    error = result.json()["error"]
    assert error["code"] == "ERR-APP-003"
    assert error["details"]["reason"] == "benefit_certificate_not_yours"


async def test_the_wired_seam_matched_passes_step_3b(
    db: AsyncSession,
    applicant_client,
    draft_ready_for_submission,
    benefit_doc_type_item_id: uuid.UUID,
    hodim_user: User,
) -> None:
    """A REAL, ACTIVE `beekeepers` row under the APPLICANT's OWN PINFL: the
    claim is `matched` and step 3b passes it through — the submission still
    ends 422 downstream (no seeded apiary tariff carries a `beekeeping_
    union_member` modifier for a GRAZING filing, the same reason every other
    "past step 3b" test in this file ends 422), which is exactly why the
    committed STATE of a matched claim is proven separately, in-process,
    below (`test_the_wired_seam_matched_verifies_the_claim_on_the_spot`) —
    the pricing failure rolls the whole transaction back before this route
    ever gets to observe the row it set."""
    item_id = await _benefit_category_item_id(db, "beekeeping_union_member")
    pinfl = (await applicant_client.get(f"{API}/auth/me")).json()["applicant"]["pinfl"]
    certificate_no = f"BEE-{uuid.uuid4().hex[:10]}"
    await beekeepers_service.create_beekeeper(
        db,
        data=BeekeeperCreateIn(
            certificate_no=certificate_no,
            pinfl=pinfl,
            passport_series="AB",
            passport_number="1234567",
            full_name="The Applicant",
        ),
        actor=hodim_user,
    )
    await db.commit()

    app_id = draft_ready_for_submission
    await _claim_and_prove(
        applicant_client,
        app_id,
        benefit_category_item_id=item_id,
        benefit_doc_type_item_id=benefit_doc_type_item_id,
        certificate_no=certificate_no,
    )

    result = await _submit(applicant_client, app_id)
    assert result.status_code == 422, result.text
    body = result.json()
    assert body["error"]["code"] != "ERR-APP-003"


async def test_the_wired_seam_matched_verifies_the_claim_on_the_spot(
    db: AsyncSession,
    applicant,
    draft_ready_for_submission: str,
    hodim_user: User,
) -> None:
    """In-process, so the committed COLUMNS can be asserted directly — the
    HTTP test above cannot, because the same request's downstream pricing
    failure rolls the whole transaction back. `service._open_benefit_
    verification` is called exactly as `submit`'s own step 3b calls it, over
    the REAL registered verifier (`register_event_subscriptions()` runs
    autouse, so `BENEFIT_AUTO_VERIFIERS["beekeeping_union_member"]` already
    IS `beekeepers.service.match_certificate` here, no stub)."""
    assert applicant.pinfl is not None
    item_id = await _benefit_category_item_id(db, "beekeeping_union_member")
    certificate_no = f"BEE-{uuid.uuid4().hex[:10]}"
    await beekeepers_service.create_beekeeper(
        db,
        data=BeekeeperCreateIn(
            certificate_no=certificate_no,
            pinfl=applicant.pinfl,
            passport_series="AB",
            passport_number="1234567",
            full_name=applicant.name,
        ),
        actor=hodim_user,
    )
    await db.flush()

    application = await service.get(db, uuid.UUID(draft_ready_for_submission))
    assert application is not None
    application.benefit_category_item_id = item_id
    application.benefit_certificate_no = certificate_no
    await db.flush()

    await service._open_benefit_verification(db, application)

    assert application.benefit_verification_status == "verified"
    assert application.benefit_verified_by is None, (
        "ruling #182: NULL means the register, not a human"
    )
    assert application.benefit_verified_at is not None
    assert application.benefit_rejection_reason is None
