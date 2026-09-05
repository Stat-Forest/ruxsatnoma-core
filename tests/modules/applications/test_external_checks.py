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
"""

import pytest


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
    same envelope (lesson: "Two mechanisms refusing one thing"), so a typo in
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
