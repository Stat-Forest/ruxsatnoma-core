"""The versioned-number API: maker-checker, effective periods, retroactivity."""

from datetime import date, timedelta

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_a_maker_creates_a_draft_and_cannot_publish_it(
    tariffs_maker_client: AsyncClient, unique_suffix: str
) -> None:
    created = await tariffs_maker_client.post(
        "/api/v1/rule-parameters",
        json={
            "code": f"test_param_{unique_suffix}",
            "value": "0.9",
            "unit": None,
            "effective_from": "2030-01-01",
            "effective_to": None,
            "basis": "test",
        },
    )
    assert created.status_code == 201
    assert created.json()["status"] == "draft"

    refused = await tariffs_maker_client.post(
        f"/api/v1/rule-parameters/{created.json()['id']}/publish"
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "ERR-NORM-005"
    assert refused.json()["error"]["details"]["reason"] == "not_maker_checker"


async def test_a_checker_publishes_the_draft(
    tariffs_maker_client: AsyncClient, tariffs_checker_client: AsyncClient, unique_suffix: str
) -> None:
    created = await tariffs_maker_client.post(
        "/api/v1/rule-parameters",
        json={
            "code": f"test_param_{unique_suffix}",
            "value": "0.9",
            "effective_from": "2030-01-01",
            "basis": "test",
        },
    )
    published = await tariffs_checker_client.post(
        f"/api/v1/rule-parameters/{created.json()['id']}/publish"
    )
    assert published.status_code == 200
    assert published.json()["item"]["status"] == "published"
    assert published.json()["warnings"] == []


async def test_publishing_an_overlapping_period_is_a_409(
    tariffs_maker_client: AsyncClient, tariffs_checker_client: AsyncClient, unique_suffix: str
) -> None:
    """The EXCLUDE constraint is the backstop; the service must answer 409 before
    it fires, so the client sees a domain error and not an IntegrityError 500."""
    code = f"test_param_{unique_suffix}"
    for payload in (
        {"code": code, "value": "1", "effective_from": "2030-01-01", "basis": "t"},
        {"code": code, "value": "2", "effective_from": "2030-06-01", "basis": "t"},
    ):
        created = await tariffs_maker_client.post("/api/v1/rule-parameters", json=payload)
        response = await tariffs_checker_client.post(
            f"/api/v1/rule-parameters/{created.json()['id']}/publish"
        )
    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "period_overlap"


async def test_publishing_with_a_past_effective_date_warns_ri04(
    tariffs_maker_client: AsyncClient, tariffs_checker_client: AsyncClient, unique_suffix: str
) -> None:
    """Ruling 11: retroactivity is legal and loud, not forbidden."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    created = await tariffs_maker_client.post(
        "/api/v1/rule-parameters",
        json={
            "code": f"test_param_{unique_suffix}",
            "value": "1",
            "effective_from": yesterday,
            "basis": "t",
        },
    )
    published = await tariffs_checker_client.post(
        f"/api/v1/rule-parameters/{created.json()['id']}/publish"
    )
    assert published.status_code == 200
    assert [w["code"] for w in published.json()["warnings"]] == ["RI-04"]


async def test_a_published_parameter_cannot_be_edited(
    tariffs_maker_client: AsyncClient, tariffs_checker_client: AsyncClient, unique_suffix: str
) -> None:
    created = await tariffs_maker_client.post(
        "/api/v1/rule-parameters",
        json={
            "code": f"test_param_{unique_suffix}",
            "value": "1",
            "effective_from": "2030-01-01",
            "basis": "t",
        },
    )
    await tariffs_checker_client.post(f"/api/v1/rule-parameters/{created.json()['id']}/publish")
    patched = await tariffs_maker_client.patch(
        f"/api/v1/rule-parameters/{created.json()['id']}", json={"value": "2"}
    )
    assert patched.status_code == 409
    assert patched.json()["error"]["details"]["reason"] == "not_draft"


async def test_the_list_filters_by_code_and_pages(
    tariffs_maker_client: AsyncClient, unique_suffix: str
) -> None:
    code = f"test_param_{unique_suffix}"
    for effective_from in ("2030-01-01", "2031-01-01", "2032-01-01"):
        await tariffs_maker_client.post(
            "/api/v1/rule-parameters",
            json={"code": code, "value": "1", "effective_from": effective_from, "basis": "t"},
        )
    listed = await tariffs_maker_client.get(f"/api/v1/rule-parameters?code={code}&limit=2")
    body = listed.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


async def test_reading_a_parameter_needs_no_special_permission(
    applicant_client: AsyncClient,
) -> None:
    """`GET /rule-parameters` is how a front-end explains a calculation; any
    authenticated user may read it. Writing is what the permission gates."""
    listed = await applicant_client.get("/api/v1/rule-parameters?code=bhm")
    assert listed.status_code == 200
    assert listed.json()["total"] >= 2


async def test_a_second_maker_cannot_publish_another_makers_draft(
    tariffs_maker_client: AsyncClient,
    second_tariffs_maker_client: AsyncClient,
    unique_suffix: str,
) -> None:
    """C1 (final review): `norms.tariffs.publish` gated nothing. The route
    accepts either tariff permission on purpose (so a maker reaches a domain
    answer instead of a bare 403 — `refs_router.py`'s module docstring), and
    `publish_versioned`'s only identity rule is `created_by != actor`. Two
    makers, neither a checker, therefore satisfied both and put an arbitrary
    rate into force: maker B drafts, maker A publishes, and the DB
    `maker_checker` CHECK passes too. Nothing ever asked whether either of
    them held the checker's own permission.

    No fixture could catch this before: `tariffs_checker_client` holds BOTH
    codes, so the two permissions were indistinguishable on this route."""
    created = await tariffs_maker_client.post(
        "/api/v1/rule-parameters",
        json={
            "code": f"test_param_{unique_suffix}",
            "value": "0.9",
            "effective_from": "2030-01-01",
            "basis": "test",
        },
    )
    assert created.status_code == 201, created.text

    refused = await second_tariffs_maker_client.post(
        f"/api/v1/rule-parameters/{created.json()['id']}/publish"
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "ERR-ACL-001"

    # And it really is still a draft — the refusal is the state, not just the
    # status code.
    listed = await tariffs_maker_client.get(
        f"/api/v1/rule-parameters?code=test_param_{unique_suffix}"
    )
    assert [item["status"] for item in listed.json()["items"]] == ["draft"]


async def test_a_maker_cannot_publish_a_migration_seeded_draft(
    tariffs_maker_client: AsyncClient,
) -> None:
    """C1's live half. A migration-seeded row has `created_by IS NULL`, so
    `publish_versioned`'s identity check is skipped entirely — there is no
    second person for a seeded row, and the publish permission was the only
    remaining control. Without it a single `TARIFFS_MANAGE` holder could put
    the ten provisional `coef_sb:*` drafts into force single-handedly, the
    exact numbers ruling 8 says must wait for VMQ 689 annex 5 and then be
    published by a different user than their creator."""
    listed = await tariffs_maker_client.get(
        "/api/v1/rule-parameters?code=coef_sb:cattle_adult&status=draft"
    )
    seeded = [item for item in listed.json()["items"] if item["created_by"] is None]
    assert seeded, "migration 0012 seeds coef_sb:cattle_adult as a draft"

    refused = await tariffs_maker_client.post(f"/api/v1/rule-parameters/{seeded[0]['id']}/publish")
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "ERR-ACL-001"
