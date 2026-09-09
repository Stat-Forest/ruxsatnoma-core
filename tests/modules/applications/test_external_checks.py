"""External checks — veterinary and cadastre (plan 03.9b task 7, tz/04 С5).

`POST /applications/{id}/checks` either calls the live vet/cadastre adapter
(`source="external_api"`) or records a paper result under maker-checker
(`source="manual_fallback"`) when the registry cannot be reached. The two
adapters (`integrations/adapters/vet.py`, `cadastre.py`) follow the same
Protocol + mock + factory shape as every other adapter in that package
(`oneid.py`, `otp_sender.py`): `VET_MODE`/`CADASTRE_MODE` default to mock, and
`real` refuses outright — `design/04` covers only the four v1 systems, and
tz/09 lists both of these at medium priority with no verified contract.

The paper fallback is maker-checker (Oybek's ruling, 2026-09-05): the row is
written `confirmed_by=None` and is not usable until a DIFFERENT reviewer
confirms it through `POST .../checks/{id}/confirm` — the same rule stage
3.7's tariff publication applies (`norms.service.publish_versioned`), proven
here the same way: `hodim_client` and `second_hodim_client` hold the
IDENTICAL permission (`applications.review`) and differ only in `users.id`,
so a passing/failing pair can only be about identity, never permission.

**Fix round 1 (2026-09-05 controller ruling)**: a mock verdict is written
`source="external_api"` exactly like a genuine one — indistinguishable on the
wire but for a buried `details.note` — so `get_adapter()` must refuse to
ANSWER a check with a mock verdict once `app_env=prod`, not only once
`VET_MODE`/`CADASTRE_MODE="real"`. Prod may still START in mock (nothing on
`docs/plan.md` stage 5 schedules either as real); it may not answer from a
fixture. `_prod_settings()` below builds a fully valid `app_env=prod`
`Settings` the same way `tests/test_config.py::
test_prod_accepts_custom_secret_key` does, and every test using it patches
ONLY `vet.get_settings`/`cadastre.get_settings` — never the app-wide
`app.config.get_settings` a whole HTTP request also depends on for cookies,
CORS and every other adapter — so an already-issued session cookie stays
valid across the call.
"""

import pytest


def _prod_settings():
    """A fully valid `app_env=prod` `Settings` — `vet_mode`/`cadastre_mode`
    left at their default `mock` (neither is in the prod mocked-adapter
    list), everything else exactly as `test_config.py::
    test_prod_accepts_custom_secret_key` fills it, so constructing it raises
    nothing of its own."""
    from app.config import Settings

    return Settings(
        app_env="prod",
        secret_key="a-real-secret-value",
        s3_secret_key="a-real-s3-secret-value",
        oneid_mode="real",
        eimzo_mode="real",
        # Stage 5.1: a real OneID refuses to construct without its
        # credentials, a real scope and a non-local redirect URI.
        oneid_client_id="forestry_uz",
        oneid_client_secret="a-real-oneid-client-secret",
        oneid_scope="forestry_uz",
        oneid_redirect_uri="https://ruxsatnoma.example.uz/api/v1/auth/oneid/callback",
        sms_mode="real",
        email_mode="real",
        payme_mode="real",
        eskiz_email="bot@example.uz",
        eskiz_password="a-real-eskiz-password",
        eskiz_sender="4546",
        eskiz_callback_secret="a-real-callback-secret",
        eskiz_callback_base_url="https://ruxsatnoma.example.uz",
        public_base_url="https://ruxsatnoma.example.uz",
        smtp_host="smtp.example.uz",
        smtp_from="noreply@example.uz",
        payme_merchant_id="a-real-merchant-id",
        payme_cashbox_key="a-real-cashbox-key",
        _env_file=None,  # pyright: ignore[reportCallIssue]
    )


async def test_the_mock_veterinary_check_records_its_verdict(
    hodim_client, application_in_review
) -> None:
    result = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks",
        json={"check_type": "vet"},
    )
    assert result.status_code == 201, result.text
    body = result.json()
    assert body["check_type"] == "vet"
    assert body["source"] == "external_api"
    assert body["result"] in ("pass", "fail", "warning")


async def test_the_real_adapter_refuses_until_a_contract_exists(monkeypatch) -> None:
    """Ruling 4: tz/09 lists the veterinary AT at medium priority with no
    verified contract, and design/04 covers only the four v1 systems. A 'real'
    adapter written against a contract nobody has read is worse than none."""
    from app.config import get_settings
    from app.modules.integrations.adapters import vet

    monkeypatch.setenv("VET_MODE", "real")
    get_settings.cache_clear()
    try:
        with pytest.raises(NotImplementedError, match="contract"):
            vet.get_adapter()
    finally:
        get_settings.cache_clear()


async def test_the_real_cadastre_adapter_also_refuses_until_a_contract_exists(
    monkeypatch,
) -> None:
    """`cadastre.py`'s own copy of the same refusal (tz/09 row 5) — proven
    separately because it is a SEPARATE `*_MODE` setting and a separate
    module, not a shared branch `vet.py`'s own test would also exercise."""
    from app.config import get_settings
    from app.modules.integrations.adapters import cadastre

    monkeypatch.setenv("CADASTRE_MODE", "real")
    get_settings.cache_clear()
    try:
        with pytest.raises(NotImplementedError, match="contract"):
            cadastre.get_adapter()
    finally:
        get_settings.cache_clear()


async def test_a_mock_vet_adapter_is_refused_under_app_env_prod(monkeypatch) -> None:
    """The controller's finding: with `vet_mode` outside the prod mocked-list,
    a prod instance starts happily at `VET_MODE=mock` (its default) and
    `MockVetAdapter.check()` would answer `result="pass"` exactly as a real
    registry might — nothing on the wire tells them apart. `get_adapter()`
    itself must refuse before that verdict is ever produced."""
    from app.modules.integrations.adapters import vet

    monkeypatch.setattr(vet, "get_settings", _prod_settings)
    with pytest.raises(NotImplementedError, match="app_env=prod"):
        vet.get_adapter()


async def test_a_mock_cadastre_adapter_is_refused_under_app_env_prod(monkeypatch) -> None:
    """`cadastre.py`'s own copy of the same refusal — a separate module and a
    separate `*_mode` setting, so proven separately (`vet.py`'s own
    `test_the_real_cadastre_adapter_also_refuses_until_a_contract_exists`
    above is the same pairing for the `real`-mode refusal)."""
    from app.modules.integrations.adapters import cadastre

    monkeypatch.setattr(cadastre, "get_settings", _prod_settings)
    with pytest.raises(NotImplementedError, match="app_env=prod"):
        cadastre.get_adapter()


async def test_mock_adapters_are_still_returned_under_dev(monkeypatch) -> None:
    """The prod refusal above must not weaken dev: `mock` stays the working
    default everywhere this whole suite already runs (`APP_ENV=dev` in
    `.env`) — `test_the_mock_veterinary_check_records_its_verdict` proves this
    at the HTTP level; this is the same fact at the adapter's own boundary,
    for both modules explicitly."""
    from app.config import get_settings
    from app.modules.integrations.adapters import cadastre, vet

    monkeypatch.setenv("APP_ENV", "dev")
    get_settings.cache_clear()
    try:
        assert isinstance(vet.get_adapter(), vet.MockVetAdapter)
        assert isinstance(cadastre.get_adapter(), cadastre.MockCadastreAdapter)
    finally:
        get_settings.cache_clear()


async def test_the_paper_fallback_still_works_when_prod_refuses_the_live_adapter(
    monkeypatch, hodim_client, application_in_review, vet_certificate_file
) -> None:
    """Ruling point 3, proven end to end: in `app_env=prod` the LIVE check is
    refused — final whole-branch review, IMPORTANT: `get_adapter()`'s bare
    `NotImplementedError` is now caught at `service._external_check` and
    turned into an honest 409 `ERR-APP-004` (`reason="live_check_unavailable"`),
    never the uncaught `ERR-SYS-001` 500 with a traceback this used to pin as
    correct — a crash sends the operator to file a bug instead of reaching for
    the paper fallback that already exists. Only `vet`/`cadastre`'s OWN
    `get_settings` is patched, never the app-wide one `hodim_client`'s session
    cookie and every other dependency also read, so nothing else about this
    request changes."""
    from app.modules.integrations.adapters import cadastre, vet

    monkeypatch.setattr(vet, "get_settings", _prod_settings)
    monkeypatch.setattr(cadastre, "get_settings", _prod_settings)

    live = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks",
        json={"check_type": "vet"},
    )
    assert live.status_code == 409, live.text
    assert live.json()["error"]["details"]["reason"] == "live_check_unavailable"

    paper = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks",
        json={
            "check_type": "vet",
            "source": "manual_fallback",
            "result": "pass",
            "doc_file_id": str(vet_certificate_file.id),
        },
    )
    assert paper.status_code == 201, paper.text


async def test_a_paper_fallback_needs_a_document_and_a_second_person(
    db, hodim_client, second_hodim_client, application_in_review, vet_certificate_file
) -> None:
    """Ruling 5 + tz/04 С5: a paper result under maker-checker. The maker must
    not be able to confirm their own record — the same rule stage 3.7 applies
    to publishing a tariff."""
    without_doc = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks",
        json={"check_type": "vet", "source": "manual_fallback", "result": "pass"},
    )
    assert without_doc.status_code == 422

    recorded = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks",
        json={
            "check_type": "vet",
            "source": "manual_fallback",
            "result": "pass",
            "doc_file_id": str(vet_certificate_file.id),
        },
    )
    assert recorded.status_code == 201, recorded.text
    check_id = recorded.json()["id"]
    assert recorded.json()["confirmed_by"] is None

    self_confirm = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks/{check_id}/confirm"
    )
    assert self_confirm.status_code == 409, "the maker may not confirm their own record"

    confirmed = await second_hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks/{check_id}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text


async def test_confirming_a_check_that_is_not_a_paper_fallback_is_refused(
    hodim_client, second_hodim_client, application_in_review
) -> None:
    """Maker-checker exists for the paper fallback alone — an
    `source="external_api"` row has no maker to distrust and nothing here
    ever writes `confirmed_by` on one."""
    recorded = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks",
        json={"check_type": "cadastre"},
    )
    assert recorded.status_code == 201, recorded.text
    check_id = recorded.json()["id"]

    result = await second_hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks/{check_id}/confirm"
    )
    assert result.status_code == 409, result.text
    assert result.json()["error"]["details"]["reason"] == "not_manual_fallback"


async def test_confirming_an_already_confirmed_check_is_refused(
    hodim_client, second_hodim_client, application_in_review, vet_certificate_file
) -> None:
    """Evidence, once confirmed, does not change hands again — the SAME
    `confirmed_by` a reviewer trusted must not become a later confirmer's,
    silently."""
    recorded = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks",
        json={
            "check_type": "cadastre",
            "source": "manual_fallback",
            "result": "warning",
            "doc_file_id": str(vet_certificate_file.id),
        },
    )
    check_id = recorded.json()["id"]

    first = await second_hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks/{check_id}/confirm"
    )
    assert first.status_code == 200, first.text

    second = await second_hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks/{check_id}/confirm"
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["details"]["reason"] == "already_confirmed"


async def test_recording_a_check_outside_the_actor_zone_is_refused(
    db, other_zone_hodim_client, application_in_review
) -> None:
    """`other_zone_hodim_client` holds the IDENTICAL `applications.review`
    permission as `hodim_client` and differs only in the leshoz it is posted
    to — a refusal here can only be about territory (lesson: zone scoping is
    not a permission check).

    A bare 404 + `ERR-SYS-003` is not enough to prove THIS mechanism fired:
    `app/main.py`'s own catch-all turns an unregistered route into the exact
    same envelope (lesson: "A green test proves nothing until you have seen it
    go red"), so a typo in
    the route path would pass this assertion too. The RI-12 audit trail
    (`test_the_out_of_zone_refusal_is_territorial_and_leaves_an_ri_12_trail`'s
    own shape) only exists if `_assert_in_actor_zone` actually ran."""
    import uuid as _uuid

    from sqlalchemy import select

    from app.modules.applications.service import APPLICATION_CHECK_ADD
    from app.modules.audit.models import AuditLog

    result = await other_zone_hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks",
        json={"check_type": "vet"},
    )
    assert result.status_code == 404, result.text
    assert result.json()["error"]["code"] == "ERR-SYS-003"

    entry = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == APPLICATION_CHECK_ADD,
                AuditLog.object_id == _uuid.UUID(application_in_review),
            )
        )
    ).scalar_one()
    assert entry.result == "denied"
    assert entry.basis == "out_of_zone"


async def test_an_adapter_call_is_written_to_the_integration_log(
    db, hodim_client, application_in_review
) -> None:
    from sqlalchemy import func, select

    from app.modules.integrations.models import IntegrationLog

    before = await db.scalar(select(func.count()).select_from(IntegrationLog))
    await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/checks",
        json={"check_type": "cadastre"},
    )
    after = await db.scalar(select(func.count()).select_from(IntegrationLog))
    assert after == before + 1
