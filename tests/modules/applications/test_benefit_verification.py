"""The central benefit-verification office (decisions.md #179; wave-2 track
T9, `docs/plans/09-odilxon-demo-fixes.md`).

**Why the pending-claim application below is stamped directly, not filed
through a real submission.** `applications.service.submit` is the file this
track may not touch, and it does not yet compute `benefit_verification_
status` at all (this module's own report to the integrator names exactly
where that belongs). `benefit_categories` also ships EMPTY — ruling #179's
own closing paragraph, VMQ 278 section IV has not arrived — so no real
category with `props.requires_certificate = true` exists in a fresh database
either. There is therefore no REAL transition yet to build the fixture
through (the lesson's own exception: "reaches that state by running the code
that produces it" only applies where such code exists) — `_make_pending_
claim` below stamps a genuinely SUBMITTED application (built through `POST
/applications` + `PATCH` + the real ERI submission, `test_submit.py::
_submit`) with the five columns exactly as `submit()` is asked to compute
them, so every test past that point exercises the real read/write surface
this track owns (`repo.py`, `benefit_verification.py`, the router) rather
than a shortcut through it.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import make_session_factory, uuid7
from app.modules.applications.benefit_verification import (
    BENEFIT_CLAIM_REJECT,
    BENEFIT_CLAIM_VERIFY,
)
from app.modules.applications.models import Application
from app.modules.audit.models import AuditLog
from tests.modules.applications.conftest import _head_client
from tests.modules.applications.test_submit import _submit
from tests.modules.auth.test_sessions import make_user

API = "/api/v1"


@pytest.fixture
async def certificate_benefit_category_item_id(engine) -> AsyncIterator[uuid.UUID]:
    """A `benefit_categories` item whose `props` carries `requires_certificate
    = true` — the shape ruling #179 keys `benefit_verification_status=
    'pending'` on. Same own-session-plus-teardown shape as conftest's own
    `benefit_category_item_id` fixture, and deliberately not that one with
    `props` patched in: a certificate-requiring category is this file's own
    concern alone."""
    item_id = uuid7()
    factory = make_session_factory(engine)
    async with factory() as own_db:
        await own_db.execute(
            text(
                "INSERT INTO classifier_items "
                "(id, classifier_id, code, name, props, valid_from, sort_order, status) "
                "SELECT :id, c.id, :code, CAST(:name AS jsonb), CAST(:props AS jsonb), "
                "DATE '2020-01-01', 0, 'active' "
                "FROM classifiers c WHERE c.code = 'benefit_categories'"
            ).bindparams(
                id=item_id,
                code=f"beekeeper_{uuid.uuid4().hex[:8]}",
                name='{"en": "Beekeeper (test)"}',
                props='{"requires_certificate": true}',
            )
        )
        await own_db.commit()
        try:
            yield item_id
        finally:
            # A test that FAILED mid-way leaves `db`'s transaction open with
            # an uncommitted application still pointing at this item, and the
            # DELETE below then waits on that transaction for ever — the whole
            # xdist worker hung 17 minutes on the stage 10 integration branch
            # before anyone read `pg_stat_activity`. A bounded wait turns the
            # hang into the failure it already is.
            await own_db.execute(text("SET LOCAL lock_timeout = '5s'"))
            await own_db.execute(
                text(
                    "UPDATE applications SET benefit_category_item_id = NULL "
                    "WHERE benefit_category_item_id = :id"
                ).bindparams(id=item_id)
            )
            await own_db.execute(
                text("DELETE FROM classifier_items WHERE id = :id").bindparams(id=item_id)
            )
            await own_db.commit()


async def _submitted_second_application(
    applicant_client, published_contour, grazing_activity_id, sheep_type_id
) -> str:
    """A SECOND real submission on `submitted_application`'s own contour and
    activity, over a period that does NOT overlap its 2027-05-01..2027-09-30
    (`ex_applications_no_duplicate`, ruling 6) — so a test naming both
    `submitted_application` and this one gets two genuinely independent rows
    rather than a duplicate-guard `IntegrityError`."""
    from tests.modules.applications.conftest import _ready_draft

    draft_id = await _ready_draft(
        applicant_client,
        published_contour.id,
        grazing_activity_id,
        sheep_type_id,
        period_from="2028-05-01",
        period_to="2028-09-30",
    )
    result = await _submit(applicant_client, draft_id)
    assert result.status_code == 200, result.text
    return draft_id


async def _make_pending_claim(
    db: AsyncSession, application_id: str, benefit_item_id: uuid.UUID, *, certificate_no: str
) -> None:
    """Stamp a real SUBMITTED application with a certificate-bearing claim —
    this module's own docstring explains why this is a direct write rather
    than a second real submission. Flushed, not committed: every client in
    this file carries `_commit_pending_before_requests` (conftest), which
    commits `db` right before its own next request — the same convention
    every fixture in this package already relies on."""
    application = await db.get(Application, uuid.UUID(application_id))
    assert application is not None
    application.benefit_category_item_id = benefit_item_id
    application.benefit_certificate_no = certificate_no
    application.benefit_verification_status = "pending"
    await db.flush()


@pytest.fixture
async def benefit_verifier_client(db: AsyncSession):
    """A user under a PRODUCTION role that holds `benefits.verify` —
    `executor_staff` since migration `0053` (ruling #182 moved the check to
    the leshoz; the central `benefit_verifier` became `beekeeping_registrar`
    and lost the permission). Zone-free here only because the routes under
    test are still #179's zone-less ones — wave 2 (B2) rewrites this file
    around the leshoz's own zone rule. `_head_client`'s own shape (conftest),
    not `_client_for`'s personal-grant one, because what is under test here
    includes whether the SEEDED role actually holds `benefits.verify` (lesson:
    "A role's identity and its grants have ONE source — the seeding
    migration")."""
    user = await make_user(db, role_code="executor_staff")
    async for client in _head_client(db, user):
        yield client


@pytest.fixture
async def pending_claim_application(
    db: AsyncSession,
    applicant_client,
    published_contour,
    grazing_activity_id: uuid.UUID,
    sheep_type_id: uuid.UUID,
    certificate_benefit_category_item_id: uuid.UUID,
    published_coef_sb: None,
    published_grazing_norm: uuid.UUID,
) -> str:
    """A genuinely SUBMITTED application carrying a `pending` certificate
    claim — the row every positive test in this file acts on.

    `published_coef_sb`/`published_grazing_norm` are DEPENDENCIES, not merely
    used by the body — `draft_ready_for_submission`'s own template (conftest):
    without the published rate and norm, `_submit` cannot price the draft at
    all and refuses with `ERR-NORM-004`."""
    application_id = await _submitted_second_application(
        applicant_client, published_contour, grazing_activity_id, sheep_type_id
    )
    await _make_pending_claim(
        db, application_id, certificate_benefit_category_item_id, certificate_no="CERT-0001"
    )
    return application_id


# --- Visibility: the queue, and the stranger's 404 ---------------------------


async def test_the_list_shows_only_certificate_bearing_applications(
    benefit_verifier_client, pending_claim_application, submitted_application
) -> None:
    """`submitted_application` (conftest's own, no benefit claim at all) must
    NOT appear — this is not a zone widening, a verifier sees ONLY claims
    (ruling #179)."""
    result = await benefit_verifier_client.get(f"{API}/applications/benefit-verifications")
    assert result.status_code == 200, result.text
    body = result.json()
    ids = {row["id"] for row in body["items"]}
    assert pending_claim_application in ids
    assert submitted_application not in ids


async def test_the_list_is_country_wide_not_zone_scoped(
    db: AsyncSession, pending_claim_application
) -> None:
    """A `benefit_verifier` holding NO organization/region/district still sees
    a claim filed against a leshoz it has no zone relationship to whatsoever
    — "the whole country, but only applications with this claim" (ruling
    #179), proven by a verifier who is not even in the same region as
    `published_contour`'s `leshoz`."""
    user = await make_user(db, role_code="executor_staff")
    async for client in _head_client(db, user):
        result = await client.get(f"{API}/applications/benefit-verifications")
        assert result.status_code == 200, result.text
        assert pending_claim_application in {row["id"] for row in result.json()["items"]}


async def test_a_verifier_reading_an_unrelated_application_gets_the_same_404_as_a_stranger(
    benefit_verifier_client, submitted_application
) -> None:
    """The task's own required negative test: `submitted_application` is a
    real, submitted, otherwise-fully-readable-by-staff row — and this role
    still gets exactly the answer a stranger to the whole system gets."""
    unrelated = await benefit_verifier_client.get(
        f"{API}/applications/benefit-verifications/{submitted_application}"
    )
    assert unrelated.status_code == 404
    assert unrelated.json()["error"]["code"] == "ERR-SYS-003"

    nonexistent = await benefit_verifier_client.get(
        f"{API}/applications/benefit-verifications/{uuid.uuid4()}"
    )
    assert nonexistent.status_code == 404
    assert nonexistent.json()["error"]["code"] == "ERR-SYS-003"
    # Same status, same code, same message shape both times — a real
    # application with no claim and an id that never existed must be
    # indistinguishable on the wire, or this route would be an
    # application-existence oracle.
    assert unrelated.json()["error"]["message"] == nonexistent.json()["error"]["message"]


async def test_a_verifier_cannot_verify_or_reject_an_unrelated_application(
    benefit_verifier_client, submitted_application
) -> None:
    """The write paths answer the identical 404 the read does — a verifier
    may not even learn that `submitted_application` exists by probing the
    write routes."""
    verify = await benefit_verifier_client.post(
        f"{API}/applications/benefit-verifications/{submitted_application}/verify"
    )
    assert verify.status_code == 404
    assert verify.json()["error"]["code"] == "ERR-SYS-003"

    reject = await benefit_verifier_client.post(
        f"{API}/applications/benefit-verifications/{submitted_application}/reject",
        json={"reason": "not this one"},
    )
    assert reject.status_code == 404
    assert reject.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_non_verifier_is_refused_the_whole_surface(gis_specialist_client) -> None:
    """A real staff role of the same leshoz that does NOT hold
    `benefits.verify` — `gis_specialist` since `0053` gave the permission to
    `executor_staff`/`executor_head` alone (ruling #182). The check is a code
    of its own, not folded into any staff read."""
    result = await gis_specialist_client.get(f"{API}/applications/benefit-verifications")
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "ERR-ACL-001"


async def test_an_applicant_is_refused_the_whole_surface(applicant_client) -> None:
    result = await applicant_client.get(f"{API}/applications/benefit-verifications")
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "ERR-ACL-001"


# --- The detail read -----------------------------------------------------------


async def test_the_detail_read_carries_the_certificate_and_its_documents(
    benefit_verifier_client, pending_claim_application
) -> None:
    """`documents` — ruling #179's "optional supporting file uses the
    existing document mechanism" — is present on the DETAIL read and starts
    empty (`repo.list_documents`'s own shape, nothing attached by this
    fixture)."""
    result = await benefit_verifier_client.get(
        f"{API}/applications/benefit-verifications/{pending_claim_application}"
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["id"] == pending_claim_application
    assert body["benefit_certificate_no"] == "CERT-0001"
    assert body["benefit_verification_status"] == "pending"
    assert body["benefit_verified_by"] is None
    assert body["benefit_verified_at"] is None
    assert body["documents"] == []


# --- verify --------------------------------------------------------------------


async def test_verify_moves_pending_to_verified_and_is_audited(
    db: AsyncSession, benefit_verifier_client, pending_claim_application
) -> None:
    result = await benefit_verifier_client.post(
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
    # An audited decision without its own `new_value` would be a trail that
    # records that something happened and not what — assert it, then read it.
    assert entry.new_value is not None
    assert entry.new_value["benefit_verification_status"] == "verified"


async def test_verifying_an_already_decided_claim_is_refused(
    benefit_verifier_client, pending_claim_application
) -> None:
    first = await benefit_verifier_client.post(
        f"{API}/applications/benefit-verifications/{pending_claim_application}/verify"
    )
    assert first.status_code == 200, first.text

    second = await benefit_verifier_client.post(
        f"{API}/applications/benefit-verifications/{pending_claim_application}/verify"
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ERR-APP-004"
    assert second.json()["error"]["details"]["reason"] == "not_pending"

    # A DECIDED claim leaves the office's own queue — ruling #179's list is
    # "certificate-bearing", not merely "still pending", but this is the
    # natural place to pin that a verified row no longer offers a second
    # verify/reject rather than silently no-op-ing.


# --- reject ----------------------------------------------------------------


async def test_reject_requires_a_reason(benefit_verifier_client, pending_claim_application) -> None:
    missing = await benefit_verifier_client.post(
        f"{API}/applications/benefit-verifications/{pending_claim_application}/reject", json={}
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "ERR-VAL-001"

    blank = await benefit_verifier_client.post(
        f"{API}/applications/benefit-verifications/{pending_claim_application}/reject",
        json={"reason": ""},
    )
    assert blank.status_code == 422
    assert blank.json()["error"]["code"] == "ERR-VAL-001"


async def test_reject_moves_pending_to_rejected_with_the_reason_and_is_audited(
    db: AsyncSession, benefit_verifier_client, pending_claim_application
) -> None:
    reason = "the certificate's serial does not match the registry"
    result = await benefit_verifier_client.post(
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


async def test_a_rejected_claim_still_reaches_the_office_own_list_as_its_own_history(
    benefit_verifier_client, pending_claim_application
) -> None:
    """`verification_status=rejected` is not merely a mutation nobody can see
    again — the office's own history of what it decided (module docstring)."""
    rejected = await benefit_verifier_client.post(
        f"{API}/applications/benefit-verifications/{pending_claim_application}/reject",
        json={"reason": "no matching registry entry"},
    )
    assert rejected.status_code == 200, rejected.text

    listed = await benefit_verifier_client.get(
        f"{API}/applications/benefit-verifications", params={"verification_status": "rejected"}
    )
    assert listed.status_code == 200, listed.text
    assert pending_claim_application in {row["id"] for row in listed.json()["items"]}

    still_pending = await benefit_verifier_client.get(
        f"{API}/applications/benefit-verifications", params={"verification_status": "pending"}
    )
    assert pending_claim_application not in {row["id"] for row in still_pending.json()["items"]}


# --- The patchable field itself --------------------------------------------


async def test_the_certificate_number_is_a_patchable_draft_field(applicant_client) -> None:
    """`benefit_certificate_no` round-trips through the ordinary draft PATCH —
    the one field of ruling #179's five that IS client-settable."""
    created = await applicant_client.post(f"{API}/applications", json={"on_behalf": "self"})
    assert created.status_code == 201, created.text
    application_id = created.json()["id"]
    assert created.json()["benefit_certificate_no"] is None
    assert created.json()["benefit_verification_status"] == "not_required"

    patched = await applicant_client.patch(
        f"{API}/applications/{application_id}", json={"benefit_certificate_no": "AB-12345"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["benefit_certificate_no"] == "AB-12345"
    # Never client-settable, even though it is on the same response shape —
    # ApplicationPatch's own `extra="forbid"` refuses the field by name.
    rejected = await applicant_client.patch(
        f"{API}/applications/{application_id}",
        json={"benefit_verification_status": "verified"},
    )
    assert rejected.status_code == 422


# --- The seam the whole feature hangs on (integration, stage 9 wave 2) -------
#
# Every test above stamps `pending` directly, for the reason this module's own
# docstring gives. That convenience hid the defect these two tests exist to
# pin: nothing in `submit()` ever SET `pending`, so a claim never reached the
# office built to check it, the certificate number was never demanded, and the
# only symptom was an empty verifier list — indistinguishable from a quiet day.


async def test_submitting_a_certificate_claim_opens_its_verification(
    applicant_client,
    draft_ready_for_submission,
    certificate_benefit_category_item_id: uuid.UUID,
    benefit_doc_type_item_id: uuid.UUID,
) -> None:
    """The REAL path, with the number present: step 3b passes and the
    submission moves on to pricing.

    It still ends 422, for the reason `test_submit.py::test_a_benefit_claim_
    needs_a_document_of_the_benefit_type_and_no_other` documents at length —
    decision #50 validates the benefit CODE against the tariff rows the
    request resolved, and no seeded VMQ 278 tariff carries a modifier for a
    category a test invented (`benefit_categories` ships empty, `tz/12` #2).
    What this test pins is WHICH gate answers: not `benefit_certificate_
    required` any more, which is exactly the difference between a claim that
    reached the verification step and one that was turned back before it.
    """
    from tests.modules.applications.test_submit import _upload

    app_id = draft_ready_for_submission
    patched = await applicant_client.patch(
        f"{API}/applications/{app_id}",
        json={
            "benefit_category_item_id": str(certificate_benefit_category_item_id),
            "benefit_certificate_no": "CERT-7788",
        },
    )
    assert patched.status_code == 200, patched.text
    proof = await applicant_client.post(
        f"{API}/applications/{app_id}/documents",
        json={
            "doc_type_item_id": str(benefit_doc_type_item_id),
            "file_id": await _upload(applicant_client),
        },
    )
    assert proof.status_code == 201, proof.text

    result = await _submit(applicant_client, app_id)
    assert result.status_code == 422, result.text
    body = result.json()
    assert body["error"]["details"].get("reason") != "benefit_certificate_required"
    assert body["error"]["code"] != "ERR-APP-003"


async def test_a_certificate_claim_without_its_number_is_refused_at_submission(
    applicant_client,
    draft_ready_for_submission,
    certificate_benefit_category_item_id: uuid.UUID,
    benefit_doc_type_item_id: uuid.UUID,
) -> None:
    """Same claim, proven by a document, but with no certificate number:
    refused AT SUBMISSION rather than accepted and left for a verifier to
    puzzle over a claim naming no certificate at all."""
    from tests.modules.applications.test_submit import _upload

    app_id = draft_ready_for_submission
    patched = await applicant_client.patch(
        f"{API}/applications/{app_id}",
        json={"benefit_category_item_id": str(certificate_benefit_category_item_id)},
    )
    assert patched.status_code == 200, patched.text
    proof = await applicant_client.post(
        f"{API}/applications/{app_id}/documents",
        json={
            "doc_type_item_id": str(benefit_doc_type_item_id),
            "file_id": await _upload(applicant_client),
        },
    )
    assert proof.status_code == 201, proof.text

    result = await _submit(applicant_client, app_id)
    assert result.status_code == 422, result.text
    body = result.json()
    assert body["error"]["code"] == "ERR-APP-003"
    assert body["error"]["details"]["reason"] == "benefit_certificate_required"
