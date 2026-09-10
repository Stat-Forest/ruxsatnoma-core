"""The submission — the ERI, the duplicate, the number, the calculation
(plan 03.9a task 5).

`submit` is the one action of this module that is a single transaction with a
foreign commit inside it: `signatures.service.sign()` commits the caller's
session on every refusal path (ruling 19), so the ORDER of the fourteen steps
is what protects the counter, the calculation and the status — not "the
transaction rolls back". Every test here is about one of those orderings.

Shared-test-DB discipline (lesson: the test DB is shared, persistent and never
empty): `number_counters` is keyed `RX:<year>` and accumulates across runs, so
nothing below asserts an absolute serial. The number test reads the counter
first and asserts the DELTA, which is the property ruling 5а actually promises.
"""

import uuid
from datetime import date

from app.modules.integrations.adapters.eimzo import encode_mock_signature


async def _upload(client) -> str:
    """A real file through the real route, so `media_files.uploaded_by` is the
    caller — which is what `service._own_document_file` reads. Same helper
    `test_documents.py` uses, kept local for the same reason it is local
    there."""
    result = await client.post(
        "/api/v1/files", files={"file": ("proof.pdf", b"%PDF-1.4 test", "application/pdf")}
    )
    assert result.status_code == 201, result.text
    return result.json()["id"]


async def _submit(client, app_id, pinfl=None, *, key=None, rules_accepted: bool = True):
    """`GET /package`, sign those exact bytes, `POST /submit` — the real client
    flow, and the only one that can work: a detached PKCS#7 cannot be produced
    over bytes the client has never seen.

    Two deviations from the brief's snippet, both forced by
    `signatures.service.sign` and by the shared, persistent test database:

    * **the signer's OWN pinfl, read from `GET /auth/me`, never a literal.**
      `sign()` re-proves ownership on every call (`_ownership_reason`) and a
      certificate whose PINFL is not the caller's is refused
      `certificate_pinfl_mismatch` — the fixtures randomise every PINFL,
      because `users.pinfl` is unique in a database every past run wrote to;
    * **a fresh certificate identity per call.** `certificates` is unique on
      `(serial_number, issuer)` and each row is BOUND to one user, so a literal
      `"SER-1"/"ISS-1"` binds to whoever ran first and is refused for every
      applicant afterwards — passing once and failing on the second run of the
      suite.

    `rules_accepted` defaults `True` (ruling #184): every caller of this
    helper wants the ERI path to reach past step 2, and the one test that
    wants the refusal passes `False` explicitly rather than leaving every
    OTHER caller in this suite to discover the new required field.
    """
    if pinfl is None:
        pinfl = (await client.get("/api/v1/auth/me")).json()["applicant"]["pinfl"]
    doc = (await client.get(f"/api/v1/applications/{app_id}/package")).content
    return await client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={
            "pkcs7": encode_mock_signature(
                document=doc, serial=f"SER-{uuid.uuid4().hex[:12]}", issuer="ISS-TEST", pinfl=pinfl
            ),
            "rules_accepted": rules_accepted,
        },
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
    )


async def _submit_with_button(client, app_id, *, key=None, rules_accepted: bool = True):
    """Ruling #183: `on_behalf="self"` signs with no envelope at all — the
    citizen's own button. No PKCS#7 to build, so no `GET /package` fetch is
    needed here either; the server prices and signs over what IT computes."""
    return await client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"rules_accepted": rules_accepted},
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
    )


async def test_submission_assigns_a_number_and_stores_exactly_one_calculation(
    db, applicant_client, draft_ready_for_submission
) -> None:
    from sqlalchemy import func, select

    from app.modules.norms.models import Calculation

    app_id = draft_ready_for_submission
    result = await _submit(applicant_client, app_id)
    assert result.status_code == 200, result.text

    body = result.json()
    assert body["status"] == "SUBMITTED"
    assert body["number"].startswith("RX-"), body["number"]
    assert len(body["number"]) == len("RX-2027-000001")

    stored = await db.scalar(
        select(func.count()).select_from(Calculation).where(Calculation.application_id == app_id)
    )
    assert stored == 1, "ruling 8: exactly one, written here and nowhere earlier"


async def test_a_second_application_on_an_overlapping_period_is_refused_with_the_first_number(
    applicant_client, draft_ready_for_submission, second_draft_same_contour
) -> None:
    """tz/05 invariant 1 and design/03: 409 with the existing number in details,
    so the applicant is told WHICH application already covers this period."""
    first = await _submit(applicant_client, draft_ready_for_submission)
    assert first.status_code == 200

    second = await _submit(applicant_client, second_draft_same_contour)
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "ERR-APP-002"
    assert error["details"]["existing_number"] == first.json()["number"]


async def test_an_invalid_signature_refuses_the_submission_whole(
    db, applicant_client, draft_ready_for_submission
) -> None:
    """The signature is step 8 of one transaction: a refusal there must leave no
    number allocated, no calculation stored and the status still DRAFT.

    Ruling 18 (в)'s negative pin: an envelope this broken (not even decodable
    base64url JSON) never reaches the `document_sha256` comparison at all —
    `eimzo.py::_unparseable_signature` hands back an empty `raw`, so
    `signatures.service._raised_reason` has nothing to compare and leaves the
    generic reason alone. A genuinely bad signature must never be told apart
    from a stale package as anything OTHER than "signature_invalid" — the
    paired positive is
    `test_a_price_that_moved_after_signing_is_labeled_package_changed`."""
    from sqlalchemy import func, select

    from app.modules.norms.models import Calculation

    app_id = draft_ready_for_submission
    result = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert result.status_code == 422
    error = result.json()["error"]
    assert error["code"] == "ERR-SIGN-001"
    assert error["details"]["reason"] == "signature_invalid"

    card = (await applicant_client.get(f"/api/v1/applications/{app_id}")).json()
    assert card["status"] == "DRAFT"
    assert card["number"] is None
    assert (
        await db.scalar(
            select(func.count())
            .select_from(Calculation)
            .where(Calculation.application_id == app_id)
        )
        == 0
    )


async def test_a_price_that_moved_after_signing_is_labeled_package_changed(
    applicant_client, draft_ready_for_submission, sheep_type_id
) -> None:
    """Ruling 18 (в)'s positive pin: a signature that is genuinely valid over
    the bytes the applicant saw must not be reported as a bare cryptographic
    failure once `submit` recomputes something else (`ERR-SIGN-001` with
    `details.reason == "package_changed"`, not the bare, forgery-shaped
    `"signature_invalid"`).

    The herd moves between `GET /package` and `POST /submit` — a real edit
    through the real route, the same mechanism a moving tariff or
    `rule_parameter` uses (`submit`'s own `_price()` call reads whatever is
    current when it runs) — rather than hand-editing the signed bytes, which
    would prove nothing about the code path this ruling actually fixed. 60
    head is still well inside `published_grazing_norm`'s MaxSB of 250, so
    nothing here trips a blocking check; the only thing under test is the
    stale signature."""
    app_id = draft_ready_for_submission
    pinfl = (await applicant_client.get("/api/v1/auth/me")).json()["applicant"]["pinfl"]
    doc = (await applicant_client.get(f"/api/v1/applications/{app_id}/package")).content

    patched = await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={"items": [{"livestock_type_id": str(sheep_type_id), "head_count": 60}]},
    )
    assert patched.status_code == 200, patched.text

    result = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={
            "pkcs7": encode_mock_signature(
                document=doc, serial=f"SER-{uuid.uuid4().hex[:12]}", issuer="ISS-TEST", pinfl=pinfl
            ),
            "rules_accepted": True,
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert result.status_code == 422, result.text
    error = result.json()["error"]
    assert error["code"] == "ERR-SIGN-001"
    assert error["details"]["reason"] == "package_changed"


async def test_a_tariff_published_between_package_and_submit_is_also_package_changed(
    engine, applicant_client, draft_ready_for_submission, grazing_activity_id
) -> None:
    """`tz/12` #24's own trigger, not just a herd edit: a tariff PUBLISHED in
    the gap between `GET /package` and `POST /submit` recomputes the amount
    exactly the way the herd edit above does, through the identical
    `_price()` call — this proves the SAME `content_changed_reason` label
    fires for the trigger the ruling was actually written about, not only for
    the one that happens to be cheapest to set up in a test.

    A raw UPDATE on the seeded `(grazing, small_adult)` published row, not a
    real `norms.service.publish_versioned` maker-checker cycle: that row is
    shared, singleton seed data other tests assert an exact coefficient
    against (`test_grazing_returns_all_four_groups`), so this runs on its OWN
    session (`published_coef_sb`'s own pattern) and restores the original
    value in a `finally` — never on the test's own `db`, whose rollback would
    not undo a COMMIT another connection has to see. Committed, because the
    app's `POST /submit` reads through a separate connection than this test's
    own session and would not see an uncommitted change.
    """
    from decimal import Decimal

    from sqlalchemy import text

    from app.db import make_session_factory

    app_id = draft_ready_for_submission
    pinfl = (await applicant_client.get("/api/v1/auth/me")).json()["applicant"]["pinfl"]
    doc = (await applicant_client.get(f"/api/v1/applications/{app_id}/package")).content

    factory = make_session_factory(engine)
    async with factory() as own_db:
        update = text(
            "UPDATE tariffs SET coefficient = :coefficient "
            "WHERE activity_type_id = :activity_type_id "
            "AND livestock_group = 'small_adult' AND status = 'published'"
        )
        await own_db.execute(
            update, {"coefficient": Decimal("0.5"), "activity_type_id": grazing_activity_id}
        )
        await own_db.commit()
        try:
            result = await applicant_client.post(
                f"/api/v1/applications/{app_id}/submit",
                json={
                    "pkcs7": encode_mock_signature(
                        document=doc,
                        serial=f"SER-{uuid.uuid4().hex[:12]}",
                        issuer="ISS-TEST",
                        pinfl=pinfl,
                    ),
                    "rules_accepted": True,
                },
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
        finally:
            await own_db.execute(
                update, {"coefficient": Decimal("0.10"), "activity_type_id": grazing_activity_id}
            )
            await own_db.commit()

    assert result.status_code == 422, result.text
    error = result.json()["error"]
    assert error["code"] == "ERR-SIGN-001"
    assert error["details"]["reason"] == "package_changed"


async def test_a_blocking_check_refuses_here_though_the_precheck_only_reported_it(
    applicant_client, draft_ready_for_submission, sheep_type_id
) -> None:
    """The pre-check reports, the submission refuses — the whole difference
    between the two, and the reason they share `checks.run_all`.

    `ERR-NORM-002` and not the brief's `ERR-NORM-003` (controller correction
    R4): an over-limit herd fails `norm_limit`, and ruling 21's mapping in
    `checks.first_blocking_error` sends `norm_limit -> ERR-NORM-002`.
    `ERR-NORM-003` is the season/rotation code, which nothing here violates.
    """
    app_id = draft_ready_for_submission
    await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={"items": [{"livestock_type_id": str(sheep_type_id), "head_count": 100_000}]},
    )

    reported = await applicant_client.post(f"/api/v1/applications/{app_id}/precheck")
    assert reported.status_code == 200

    refused = await _submit(applicant_client, app_id)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "ERR-NORM-002"


async def test_the_signed_package_bytes_are_stable(
    db, applicant_client, draft_ready_for_submission
) -> None:
    """A change to key order, to Decimal formatting or to which fields are
    included silently invalidates every signature ever produced. Pin it."""
    from app.modules.applications import service

    app_id = draft_ready_for_submission
    await _submit(applicant_client, app_id)
    row = await service.get(db, uuid.UUID(app_id))
    assert row is not None
    calc = await service.current_calculation(db, row.id)

    assert service._package_bytes(row, calc).startswith(b'{"activity_type_code":')


async def test_three_failed_signatures_accumulate_checks_and_nothing_else(
    db, applicant_client, draft_ready_for_submission
) -> None:
    """Raised by the 3.8 session on 2026-09-02, and worth its own test because
    ONE failed attempt does not expose it. `sign()` commits on refusal
    (ruling 19), so each attempt leaves its `application_checks` rows behind.
    Three attempts must therefore: accumulate three sets (there is deliberately
    NO unique index on (application_id, check_type) — ruling 12 makes a repeat a
    new row, and a unique index would make the SECOND attempt crash instead of
    refusing cleanly); leave the application in DRAFT with no number; and leave
    ZERO calculations, which is the property ruling 19 exists to protect."""
    import uuid

    from sqlalchemy import func, select

    from app.modules.norms.models import Calculation

    app_id = draft_ready_for_submission
    for _ in range(3):
        refused = await applicant_client.post(
            f"/api/v1/applications/{app_id}/submit",
            json={"pkcs7": "not-a-signature", "rules_accepted": True},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "ERR-SIGN-001"

    card = (await applicant_client.get(f"/api/v1/applications/{app_id}")).json()
    assert card["status"] == "DRAFT"
    assert card["number"] is None

    overlap = [c for c in card["checks"] if c["check_type"] == "gis_overlap"]
    assert len(overlap) == 3, "each real attempt is recorded; none overwrites another"

    assert (
        await db.scalar(
            select(func.count())
            .select_from(Calculation)
            .where(Calculation.application_id == app_id)
        )
        == 0
    )


async def test_the_number_is_not_consumed_by_a_failed_submission(
    db, applicant_client, draft_ready_for_submission, another_ready_draft
) -> None:
    """Ruling 5а + ruling 19's ordering: the number is allocated at step 10,
    AFTER the signature at step 8, so a refused signature never reaches the
    counter at all — and for a failure later than step 10 the counter moves
    inside the transaction, so a rollback takes it back. Both halves matter:
    with `sign()` committing on refusal, "the transaction rolls back" is NOT
    what protects the counter on this particular path, the ordering is.

    The brief asserted `endswith("000001")`. That cannot hold here: the real
    scope is `RX:<business year>` in a shared, persistent test database that
    every previous run has already written to (lesson). The DELTA is the
    property ruling 5а actually promises, so the counter is read before and
    after and the next number is derived from it.
    """
    from app.core.time import business_today

    year = business_today().year

    async def counter() -> int:
        from sqlalchemy import select

        from app.core.models import NumberCounter

        value = await db.scalar(
            select(NumberCounter.last_value).where(NumberCounter.scope == f"RX:{year}")
        )
        return value or 0

    before = await counter()
    refused = await applicant_client.post(
        f"/api/v1/applications/{draft_ready_for_submission}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert refused.status_code == 422
    assert await counter() == before, "step 8 refused before step 10 ever ran"

    good = await _submit(applicant_client, another_ready_draft)
    assert good.status_code == 200, good.text
    assert good.json()["number"] == f"RX-{year}-{before + 1:06d}"
    assert await counter() == before + 1


# --- beyond the brief's seven -------------------------------------------------


async def test_the_package_bytes_are_the_same_from_a_preview_and_from_a_stored_row(
    db, applicant_client, draft_ready_for_submission
) -> None:
    """Controller ruling R2, and the reason `_package_bytes` normalises both
    shapes instead of taking one.

    `submit` signs bytes built from `norms.service.preview`'s DICT — nothing is
    stored at signing time (ruling 19) — while a verifier years later has only
    the stored `calculations` row. If those two ever produce different bytes,
    every past signature stops verifying and nothing else in the suite would
    say so: the brief's own stability test uses the stored row alone, so it
    cannot see a divergence.

    The two disagree naturally, which is why this is not a tautology: the
    calculator's `Decimal` amount round-trips at the COLUMN's own NUMERIC scale
    (lesson), so `2060000` and `2060000.0000` are the same money written two
    ways.
    """
    from app.modules.applications import checks, service
    from app.modules.auth.models import User
    from app.modules.norms import service as norms_service

    app_id = uuid.UUID(draft_ready_for_submission)
    submitted = await _submit(applicant_client, str(app_id))
    assert submitted.status_code == 200, submitted.text

    application = await service.get(db, app_id)
    assert application is not None
    stored = await service.current_calculation(db, app_id)
    assert stored is not None

    actor = await db.get(User, application.submitted_by_user_id)
    assert actor is not None
    priced = await norms_service.preview(
        db, payload=await checks.calculation_payload(db, application), actor=actor
    )

    assert service._package_bytes(application, priced) == service._package_bytes(
        application, stored
    )


async def test_the_package_route_is_refused_to_a_stranger(
    applicant_client, other_applicant_client, draft_ready_for_submission
) -> None:
    """Ownership leaks are 404, never 403: `GET /package` would otherwise
    confirm that this id is an application AND hand over the applicant, the
    plot and the price of somebody else's filing."""
    refused = await other_applicant_client.get(
        f"/api/v1/applications/{draft_ready_for_submission}/package"
    )
    assert refused.status_code == 404


async def test_a_draft_cancelled_without_ever_being_submitted_still_serves_a_package(
    applicant_client, draft_ready_for_submission
) -> None:
    """`cancel` from DRAFT never freezes `contour_version_id` (nothing does,
    before `submit`'s step 4), so a completed-then-withdrawn draft reaches
    `package` with the column still null. Before the fix, `package` took the
    frozen-column branch for every non-DRAFT status including CANCELLED, found
    it null and `_package_bytes` answered 500 `ERR-SYS-001`
    `package_without_a_contour_version` — a real regression the fix wave's
    change to that branch introduced. The DRAFT-typed fallback resolves the
    current published version instead, the same way an actual DRAFT does."""
    app_id = draft_ready_for_submission
    cancelled = await applicant_client.post(f"/api/v1/applications/{app_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"

    result = await applicant_client.get(f"/api/v1/applications/{app_id}/package")
    assert result.status_code == 200, result.text
    assert len(result.content) > 0


async def test_a_stranger_cannot_submit_my_draft(
    other_applicant_client, draft_ready_for_submission
) -> None:
    refused = await other_applicant_client.post(
        f"/api/v1/applications/{draft_ready_for_submission}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert refused.status_code == 404


async def test_the_submit_route_requires_an_idempotency_key(
    applicant_client, draft_ready_for_submission
) -> None:
    """3.4's mechanism, mandatory here and nowhere else in this module: a
    replayed POST would otherwise mint a SECOND public number for one filing.
    `POST /applications` and the pre-check carry no key on purpose."""
    refused = await applicant_client.post(
        f"/api/v1/applications/{draft_ready_for_submission}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["reason"] == "idempotency_key_required"


async def test_a_replayed_key_returns_the_stored_response_and_not_a_second_number(
    db, applicant_client, draft_ready_for_submission
) -> None:
    """The whole reason the key is mandatory here: the replay must answer with
    the SAME number, and the counter must not have moved a second time."""
    from sqlalchemy import select

    from app.core.models import NumberCounter
    from app.core.time import business_today

    key = str(uuid.uuid4())
    app_id = draft_ready_for_submission
    pinfl = (await applicant_client.get("/api/v1/auth/me")).json()["applicant"]["pinfl"]
    doc = (await applicant_client.get(f"/api/v1/applications/{app_id}/package")).content
    body = {
        "pkcs7": encode_mock_signature(
            document=doc, serial=f"SER-{uuid.uuid4().hex[:12]}", issuer="ISS-TEST", pinfl=pinfl
        ),
        "rules_accepted": True,
    }
    first = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit", json=body, headers={"Idempotency-Key": key}
    )
    assert first.status_code == 200, first.text
    scope = f"RX:{business_today().year}"
    after_first = await db.scalar(
        select(NumberCounter.last_value).where(NumberCounter.scope == scope)
    )

    replay = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit", json=body, headers={"Idempotency-Key": key}
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["number"] == first.json()["number"]
    assert (
        await db.scalar(select(NumberCounter.last_value).where(NumberCounter.scope == scope))
        == after_first
    )


async def test_a_same_key_retry_after_a_refused_submission_replays_it_not_in_flight(
    applicant_client,
) -> None:
    """The applicant-visible half of `core/idempotency.py`'s exception-path fix
    (3.9b task 3, ANSWERED а, 2026-09-05). `_submit` above mints a FRESH key on
    EVERY call, which is exactly how this defect stayed hidden: a route that
    RAISED instead of returning left `response_status` NULL, so the very next
    call with the SAME key hit 409 `in_flight` for the whole `IN_FLIGHT_TTL` —
    even though the refusal (an incomplete draft, here) was the applicant's own
    to fix, the entire point of reusing an idempotency key.

    A SAME-key retry must replay the identical 400 `ERR-APP-001`; a DIFFERENT
    key must proceed on its own merits rather than being caught up in the
    first attempt's failure.
    """
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]
    key = str(uuid.uuid4())

    first = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 400, first.text
    assert first.json()["error"]["code"] == "ERR-APP-001"

    replay = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 400, replay.text
    assert replay.json() == first.json(), "the SAME key must replay, never hit in_flight"

    retried = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert retried.status_code == 400, retried.text
    assert retried.json()["error"]["code"] == "ERR-APP-001"


async def test_a_same_key_retry_after_a_malformed_body_replays_the_422(
    applicant_client,
) -> None:
    """The `RequestValidationError` handler's own half of the fix (3.9b task
    3, fix round 1, 2026-09-05): `idempotency_context` is a SIBLING
    dependency FastAPI resolves — and COMMITS — before it ever discovers the
    body itself is invalid. Without `_settle_idempotency_record` closing the
    record here too, the SAME key would answer 409 `in_flight` (or
    `fingerprint_mismatch` for a corrected body) for the whole
    `IN_FLIGHT_TTL` instead of replaying this 422 — the lockout through the
    OTHER door `domain_error_handler` alone left open.

    **`json={"pkcs7": 123}`, not `json={}`** (ruling #183 made `pkcs7`
    optional, so an empty body is a VALID `ApplicationSubmitIn` now —
    `rules_accepted` defaults `False` and reaches the handler as a 400
    `ERR-APP-001`, not a wire-level 422). A `pkcs7` of the wrong TYPE is
    still a pydantic `RequestValidationError`, the exact shape this test
    exists to exercise.
    """
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]
    key = str(uuid.uuid4())

    first = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": 123},  # wrong type — fails BEFORE the endpoint runs
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 422, first.text
    assert first.json()["error"]["code"] == "ERR-VAL-001"

    replay = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": 123},
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 422, replay.text
    assert replay.json() == first.json(), "the SAME key must replay, never hit in_flight"


async def test_a_same_key_retry_after_a_server_error_is_not_locked_out(
    monkeypatch, applicant_client
) -> None:
    """The catch-all `Exception` handler's own half of the fix: a 500 is the
    SERVER's failure, not the request's, so `_settle_idempotency_record`
    DELETES the marker instead of closing it — unlike the two refusal-replay
    tests above, a retry with the SAME key must proceed FRESH, never replay
    the 500 and never hit 409 `in_flight` either.

    Simulated with a monkeypatch that makes `_assert_complete` raise a bare
    `RuntimeError` instead of its own `ERR-APP-001` — a real, unhandled bug,
    not a weakened handler — so the request genuinely reaches
    `unhandled_handler`. The patch is undone before the retry so that call
    exercises the real code path and its own, unrelated 400 refusal.
    """
    from app.modules.applications import service

    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]
    key = str(uuid.uuid4())

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated bug")

    monkeypatch.setattr(service, "_assert_complete", _boom)
    first = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 500, first.text
    monkeypatch.undo()

    retry = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": key},
    )
    assert retry.status_code != 409, retry.text
    assert retry.status_code == 400, retry.text
    assert retry.json()["error"]["code"] == "ERR-APP-001"


async def test_an_incomplete_draft_is_refused_400_naming_the_missing_fields(
    applicant_client, published_contour
) -> None:
    """ERR-APP-001 is 400, not 422 — the catalogue, `tz/10` and the original
    spec all say so. The refusal NAMES the fields, because "не заполнено
    обязательное поле" without saying which one is not an answer."""
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]

    refused = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert error["code"] == "ERR-APP-001"
    assert "contour_id" in error["details"]["missing"]
    assert "activity_type_id" in error["details"]["missing"]


async def test_a_grazing_draft_with_no_herd_names_items_as_the_missing_field(
    applicant_client, published_contour, grazing_activity_id, published_coef_sb
) -> None:
    """The half of the completeness gate that is not decoration: without it
    `norms.calculator` would answer a haymaking filing with
    `ERR-VAL-001 {"reason": "quantity_required"}` from a module the applicant
    never called, instead of "you left a field empty"."""
    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]
    await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(grazing_activity_id),
            "period_from": "2027-05-01",
            "period_to": "2027-09-30",
        },
    )

    refused = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["details"]["missing"] == ["items"]


async def test_a_missing_address_is_named_in_missing_and_resolved_by_filling_it_in(
    db, applicant_client, applicant, draft_ready_for_submission
) -> None:
    """Ruling #113 (`tz/12` #20): `checks.missing_for_pricing` names `address`
    exactly like any other incomplete field — a pre-check that reported
    "ready" would otherwise be lying about the one thing task 5a's
    `PATCH /auth/applicants/{id}/address` exists to fix.

    The `applicant` fixture is deliberately named alongside `applicant_client`
    and `draft_ready_for_submission`: pytest resolves all three to the SAME
    row (fixtures are cached by name within one test), so nulling its address
    here is nulling exactly the row `applicant_client` submits as.
    """
    applicant.address = None
    await db.commit()
    app_id = draft_ready_for_submission

    refused = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["details"]["missing"] == ["address"]

    filled = await applicant_client.patch(
        f"/api/v1/auth/applicants/{applicant.id}/address",
        json={"address": "Toshkent, Mirzo Ulug'bek tumani, 3-uy"},
    )
    assert filled.status_code == 200, filled.text

    result = await _submit(applicant_client, app_id)
    assert result.status_code == 200, result.text


async def test_a_benefit_claim_is_accepted_without_a_document(
    applicant_client,
    draft_ready_for_submission,
    benefit_category_item_id,
    doc_type_item_id,
) -> None:
    """Ruling #189: the certificate's scan is optional. A claim with its
    number and NO attachment at all — or with an attachment of some other
    type, which used to be refused as "not the benefit type" — passes what
    was step 3 and is answered by the gates behind it. The refusal below is
    step 7's pricing (decision #50: no seeded tariff carries a modifier for
    a category a test invented), and what matters is which gate answers: not
    `ERR-APP-003` with a reason about documents.

    The previous version of this test (`..._needs_a_document_of_the_benefit_
    type_and_no_other`) pinned the opposite — a fail-closed document guard —
    and was deleted with the guard, together with the "unconfigured doc type"
    refusal that guard alone could raise.
    """
    app_id = draft_ready_for_submission
    await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={
            "benefit_category_item_id": str(benefit_category_item_id),
            "benefit_certificate_no": "CERT-DOC-1",
        },
    )

    bare = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert bare.status_code == 422, bare.text
    assert bare.json()["error"]["code"] == "ERR-VAL-001"

    other = await applicant_client.post(
        f"/api/v1/applications/{app_id}/documents",
        json={
            "doc_type_item_id": str(doc_type_item_id),
            "file_id": await _upload(applicant_client),
        },
    )
    assert other.status_code == 201, other.text
    with_other = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert with_other.status_code == 422, with_other.text
    assert with_other.json()["error"]["code"] == "ERR-VAL-001"


async def test_a_draft_on_an_unpublished_contour_cannot_be_submitted(
    applicant_client, contours_layer, leshoz, grazing_activity_id, sheep_type_id, db
) -> None:
    """Ruling 22 freezes `contour_version_id` and `requested_area_ha` from the
    contour's PUBLISHED version, so a contour whose geometry is still a draft
    has nothing to freeze — and `max_approve_area` would compare against NULL
    for the rest of that application's life. 409, a state conflict of a GIS
    object."""
    from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt

    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(db, contour.id, random_box_wkt(), status="draft")
    await db.flush()

    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]
    await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={
            "contour_id": str(contour.id),
            "activity_type_id": str(grazing_activity_id),
            "period_from": "2027-05-01",
            "period_to": "2027-09-30",
            "items": [{"livestock_type_id": str(sheep_type_id), "head_count": 40}],
        },
    )

    refused = await applicant_client.post(
        f"/api/v1/applications/{app_id}/submit",
        json={"pkcs7": "not-a-signature", "rules_accepted": True},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["details"]["reason"] == "no_published_version"


async def test_a_submitted_application_cannot_be_submitted_again(
    applicant_client, draft_ready_for_submission
) -> None:
    """409 `ERR-APP-004`: DRAFT is the only submittable status in 3.9a (3.9b
    adds RETURNED)."""
    first = await _submit(applicant_client, draft_ready_for_submission)
    assert first.status_code == 200, first.text

    again = await _submit(applicant_client, draft_ready_for_submission)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ERR-APP-004"


async def test_submission_freezes_the_area_the_sla_and_the_signature_identity(
    db, applicant_client, draft_ready_for_submission
) -> None:
    """Four facts of one transaction that nothing else asserts together:

    * `requested_area_ha` is the published version's own `area_ha` (ruling 22)
      — without it decision #29's `max_approve_area` compares against NULL;
    * `sla_deadline_at = submitted_at + 15 days` (ruling 13);
    * the SUBMITTED history row's id IS the signature's `object_id`
      (ruling 25), so a resubmission in 3.9b collides with nothing;
    * the audit entry is written under this module's own constant.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from app.modules.applications.models import ApplicationStatusHistory
    from app.modules.applications.service import APPLICATION_SUBMIT, SLA_DAYS
    from app.modules.audit.models import AuditLog
    from app.modules.gis import service as gis_service
    from app.modules.signatures.models import Signature

    app_id = uuid.UUID(draft_ready_for_submission)
    assert (await _submit(applicant_client, str(app_id))).status_code == 200

    from app.modules.applications import service

    application = await service.get(db, app_id)
    assert application is not None
    await db.refresh(application)

    assert application.contour_id is not None
    version = await gis_service.published_version(db, application.contour_id)
    assert version is not None
    assert application.contour_version_id == version.id
    assert application.requested_area_ha == version.area_ha

    assert application.submitted_at is not None and application.sla_deadline_at is not None
    assert application.sla_deadline_at - application.submitted_at == timedelta(days=SLA_DAYS)

    history = (
        await db.scalars(
            select(ApplicationStatusHistory).where(
                ApplicationStatusHistory.application_id == app_id,
                ApplicationStatusHistory.to_status == "SUBMITTED",
            )
        )
    ).all()
    assert len(history) == 1
    signature = (
        await db.scalars(
            select(Signature).where(
                Signature.object_type == "application_submission",
                Signature.object_id == history[0].id,
            )
        )
    ).all()
    assert len(signature) == 1, "ruling 25: the ATTEMPT is signed, not the application"
    assert signature[0].purpose == "application_submit"

    audited = (
        await db.scalars(
            select(AuditLog).where(
                AuditLog.action == APPLICATION_SUBMIT, AuditLog.object_id == app_id
            )
        )
    ).all()
    assert len(audited) == 1


async def test_the_applicant_is_notified_under_the_dotted_template_code(
    db, applicant_client, draft_ready_for_submission
) -> None:
    """`notify(event_code="application.submitted")` — DOTTED. The bus name
    `application_submitted` is a different vocabulary: `notify()` on a code
    with no template writes a raw fallback string in-app and sends NOTHING by
    SMS or e-mail, silently, on every submission. This test is what makes the
    difference visible."""
    from sqlalchemy import select

    from app.modules.notifications.models import Notification

    result = await _submit(applicant_client, draft_ready_for_submission)
    assert result.status_code == 200, result.text
    number = result.json()["number"]

    rows = (
        await db.scalars(
            select(Notification).where(
                Notification.object_type == "application",
                Notification.object_id == uuid.UUID(draft_ready_for_submission),
            )
        )
    ).all()
    assert rows, "С19: the in-app row is always written"
    assert {row.event_code for row in rows} == {"application.submitted"}
    inapp = next(row for row in rows if row.channel == "inapp")
    # A template was actually FOUND: `notify()` answers a code with no template
    # by writing a raw fallback string with `template_id` null, silently, and
    # sending nothing at all by SMS or e-mail. That is exactly what passing the
    # flat bus name `application_submitted` here would do.
    assert inapp.template_id is not None
    assert number in inapp.rendered_text


async def test_the_submission_publishes_application_id_and_nothing_else(
    db, applicant_client, draft_ready_for_submission, monkeypatch
) -> None:
    """`applications/events.py`'s payload contract is frozen: every one of the
    four names carries `application_id` and NOTHING else — not the public
    `number`, however convenient (controller ruling R14). A subscriber reads
    the rest through `applications.service.get`."""
    from app.core import events
    from app.modules.applications.events import APPLICATION_SUBMITTED

    seen: list[dict] = []

    async def _spy(_db, event):
        seen.append(dict(event.payload))

    monkeypatch.setitem(
        events._SUBSCRIBERS,
        APPLICATION_SUBMITTED,
        [*events._SUBSCRIBERS.get(APPLICATION_SUBMITTED, []), _spy],
    )

    result = await _submit(applicant_client, draft_ready_for_submission)
    assert result.status_code == 200, result.text
    assert seen == [{"application_id": uuid.UUID(draft_ready_for_submission)}]


def test_the_package_is_canonical_json_with_no_whitespace() -> None:
    """One function, sorted keys, no whitespace, UTF-8 — what a verifier
    re-derives years later. A pure unit check so a failure names the shape
    rather than a request.

    Ruling 23 is why this matters more than usual: `GET /package` and `submit`
    each price afresh, so the bytes are already allowed to differ between two
    moments in time; a change to the SHAPE would make every past signature
    unverifiable as well.
    """
    from decimal import Decimal

    from app.modules.applications import service
    from app.modules.applications.models import Application
    from app.modules.norms.models import Calculation

    # A real, never-persisted `Application`: `_package_bytes` reads five of its
    # columns and the type checker reads its signature, so a stand-in object
    # would only prove that a stand-in works.
    application = Application(
        id=uuid.UUID("00000000-0000-0000-0000-0000000000c3"),
        applicant_id=uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
        contour_version_id=uuid.UUID("00000000-0000-0000-0000-0000000000b2"),
        period_from=date(2027, 5, 1),
        period_to=date(2027, 9, 30),
        quantity=None,
    )
    priced = {
        "amount": "2060000.00",
        "rule_code_version": "norms-1.0.0",
        "input_snapshot": {
            "request": {
                "activity_code": "grazing",
                "items": [
                    {"livestock_code": "sheep_goat_6m", "count": 40},
                    {"livestock_code": "cattle_2y", "count": 2},
                ],
            }
        },
    }
    assert service._package_bytes(application, priced) == (
        b'{"activity_type_code":"grazing",'
        b'"amount":"2060000",'
        b'"applicant_id":"00000000-0000-0000-0000-0000000000a1",'
        # Review round 2, important 3: WHICH application. Without it two drafts
        # of one applicant with identical content sign to identical bytes and
        # one PKCS#7 verifies for either.
        b'"application_id":"00000000-0000-0000-0000-0000000000c3",'
        b'"contour_version_id":"00000000-0000-0000-0000-0000000000b2",'
        b'"items":[{"count":2,"livestock_code":"cattle_2y"},'
        b'{"count":40,"livestock_code":"sheep_goat_6m"}],'
        b'"period_from":"2027-05-01",'
        b'"period_to":"2027-09-30",'
        b'"quantity":null,'
        b'"rule_version":"norms-1.0.0"}'
    )
    # The same money written at the column's own NUMERIC scale is the same
    # bytes (controller ruling R2): a stored row and a preview must agree.
    stored = Calculation(
        amount=Decimal("2060000.0000"),
        rule_code_version="norms-1.0.0",
        input_snapshot=priced["input_snapshot"],
    )
    assert service._package_bytes(application, stored) == service._package_bytes(
        application, priced
    )


# --- Rulings #183/#184: the button, the legal-entity refusal, the mandatory
#     rules acceptance ------------------------------------------------------


async def test_rules_accepted_false_is_refused_naming_the_field(
    applicant_client, draft_ready_for_submission
) -> None:
    """Ruling #184: `rules_accepted` is folded into the SAME `missing` list
    `_assert_complete` already builds, not a separate check — the draft here
    is otherwise COMPLETE, so this is the one field named."""
    refused = await _submit(applicant_client, draft_ready_for_submission, rules_accepted=False)
    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert error["code"] == "ERR-APP-001"
    assert error["details"]["missing"] == ["rules_accepted"]


async def test_a_citizen_signs_with_the_button_and_it_is_a_simple_signature(
    db, applicant_client, draft_ready_for_submission
) -> None:
    """Ruling #183: `on_behalf="self"` and no `pkcs7` in the body signs with
    the button — `signatures.service.sign_simple`, over the SAME package
    bytes `sign()` would otherwise verify. `kind='simple'` on the row is the
    whole point: nothing cryptographic was checked, and the row says so
    honestly rather than pretending an ERI ran.

    Scoped to THIS submission's own history row (ruling 25: the ATTEMPT is
    signed, `object_id` is `application_status_history.id`, not the
    application) — the test DB is shared and persistent, so a query filtered
    only by `object_type`/`purpose` would match every OTHER submission ever
    signed in it.
    """
    from sqlalchemy import select

    from app.modules.applications.models import ApplicationStatusHistory
    from app.modules.signatures.models import Signature

    app_id = draft_ready_for_submission
    result = await _submit_with_button(applicant_client, app_id)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "SUBMITTED"
    assert body["rules_accepted_at"] is not None

    history = (
        await db.scalars(
            select(ApplicationStatusHistory).where(
                ApplicationStatusHistory.application_id == uuid.UUID(app_id),
                ApplicationStatusHistory.to_status == "SUBMITTED",
            )
        )
    ).one()
    signature = (
        await db.scalars(
            select(Signature).where(
                Signature.object_type == "application_submission",
                Signature.object_id == history.id,
            )
        )
    ).one()
    assert signature.kind == "simple"
    assert signature.certificate_id is None
    assert signature.verification_status == "valid"

    # The NEXT screen (stage 10 review finding 2): the timeline embeds this
    # row through `TimelineSignatureRow`, which required a certificate id — a
    # simple row has none, and the read answered 500 while `/submit` was 200.
    timeline = await applicant_client.get(f"/api/v1/applications/{app_id}/timeline")
    assert timeline.status_code == 200, timeline.text
    submitted_rows = [
        row for row in timeline.json()["status_history"] if row["to_status"] == "SUBMITTED"
    ]
    assert len(submitted_rows) == 1
    (signature_row,) = submitted_rows[0]["signatures"]
    assert signature_row["kind"] == "simple"
    assert signature_row["certificate_id"] is None
    assert signature_row["verification_status"] == "valid"


async def test_a_legal_entity_without_an_envelope_is_refused(
    representative_client,
    legal_applicant,
    published_contour,
    grazing_activity_id,
    sheep_type_id,
    published_coef_sb,
    published_grazing_norm,
) -> None:
    """Ruling #183's OTHER half: decision #9 still requires ERI of a legal
    entity's representative — the button is `on_behalf="self"` alone.

    **Not `_ready_draft`** (conftest's own helper): it hard-codes
    `on_behalf="self"`, which would file this draft under the
    representative's OWN individual `Applicant` row instead of `legal_
    applicant` — proving nothing about a legal entity at all, and failing
    for an unrelated reason (that individual row carries no `address`).
    """
    created = await representative_client.post(
        "/api/v1/applications",
        json={"on_behalf": "legal", "applicant_id": str(legal_applicant.id)},
    )
    assert created.status_code == 201, created.text
    app_id = created.json()["id"]
    patched = await representative_client.patch(
        f"/api/v1/applications/{app_id}",
        json={
            "contour_id": str(published_contour.id),
            "activity_type_id": str(grazing_activity_id),
            "period_from": "2027-05-01",
            "period_to": "2027-09-30",
            "items": [{"livestock_type_id": str(sheep_type_id), "head_count": 40}],
        },
    )
    assert patched.status_code == 200, patched.text

    refused = await _submit_with_button(representative_client, app_id)
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "ERR-SIGN-001"
    assert error["details"]["reason"] == "simple_signature_not_allowed"


async def test_the_eri_path_also_stamps_rules_accepted_at(
    applicant_client, draft_ready_for_submission
) -> None:
    """`rules_accepted_at` is not a `sign_simple`-only side effect — every
    submission stamps it, `pkcs7` present or not (step 2 runs before step 8
    either way)."""
    result = await _submit(applicant_client, draft_ready_for_submission)
    assert result.status_code == 200, result.text
    assert result.json()["rules_accepted_at"] is not None
