"""Draft → Review → Approved → Published → Archived, and who may do what.
Publication is the central office's (ruling 16) and freezes MaxSB (ruling 17)."""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.models import MediaFile, SystemSetting
from app.modules.gis.models import Contour
from tests.modules.norms.conftest import CONTOUR_AREA_HA

pytestmark = pytest.mark.asyncio


async def _draft(client: AsyncClient, contour_id: uuid.UUID, activity_id: uuid.UUID, **over):
    payload = {
        "contour_id": str(contour_id),
        "activity_type_id": str(activity_id),
        "yield_c_per_ha": "12.0",
        "season": {"windows": [{"from": "04-01", "to": "10-31"}]},
        "rotation": {"rest_years": []},
        "effective_from": "2030-01-01",
    } | over
    return await client.post("/api/v1/norms", json=payload)


async def test_the_full_cycle_publishes_and_freezes_max_sb(
    gis_specialist_client: AsyncClient,
    leadership_client: AsyncClient,
    central_admin_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    survey_doc: MediaFile,
    approval_doc: MediaFile,
) -> None:
    created = await _draft(
        gis_specialist_client,
        published_contour.id,
        grazing_activity_id,
        geobotanic_doc_id=str(survey_doc.id),
    )
    assert created.status_code == 201
    norm_id = created.json()["id"]

    assert (await gis_specialist_client.post(f"/api/v1/norms/{norm_id}/submit-review")).json()[
        "status"
    ] == "review"
    approved = await leadership_client.post(
        f"/api/v1/norms/{norm_id}/approve", json={"approval_doc_id": str(approval_doc.id)}
    )
    assert approved.json()["status"] == "approved"

    published = await central_admin_client.post(f"/api/v1/norms/{norm_id}/publish")
    assert published.status_code == 200
    body = published.json()

    # MaxSB = floor(area × yield × season_share × safety_reserve / sb_feed_norm)
    expected = int(CONTOUR_AREA_HA * Decimal("12.0") * Decimal("0.85") / Decimal("3.74"))
    assert body["max_sb"] == expected
    assert body["published_at"] is not None


async def test_the_leshoz_leadership_cannot_skip_the_review(
    leadership_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    approval_doc: MediaFile,
) -> None:
    created = await _draft(leadership_client, published_contour.id, grazing_activity_id)
    approved = await leadership_client.post(
        f"/api/v1/norms/{created.json()['id']}/approve",
        json={"approval_doc_id": str(approval_doc.id)},
    )
    assert approved.status_code == 409
    assert approved.json()["error"]["details"]["reason"] == "bad_transition"


async def test_in_central_mode_a_leshoz_actor_cannot_publish(
    gis_specialist_client: AsyncClient,
    leadership_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    survey_doc: MediaFile,
    approval_doc: MediaFile,
) -> None:
    """Ruling 16, default mode: VMQ 689 has forest-pasture norms put in force
    centrally. `leadership_client` HOLDS `norms.publish` and is still refused —
    the setting, not the grant, is what decides."""
    created = await _draft(
        gis_specialist_client,
        published_contour.id,
        grazing_activity_id,
        geobotanic_doc_id=str(survey_doc.id),
    )
    norm_id = created.json()["id"]
    await gis_specialist_client.post(f"/api/v1/norms/{norm_id}/submit-review")
    await leadership_client.post(
        f"/api/v1/norms/{norm_id}/approve", json={"approval_doc_id": str(approval_doc.id)}
    )
    refused = await leadership_client.post(f"/api/v1/norms/{norm_id}/publish")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "ERR-ACL-002"
    assert refused.json()["error"]["details"]["reason"] == "central_publication_required"


async def test_in_leshoz_mode_the_same_actor_publishes(
    db: AsyncSession,
    gis_specialist_client: AsyncClient,
    leadership_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    survey_doc: MediaFile,
    approval_doc: MediaFile,
) -> None:
    """The other half of ruling 16. The store has getters only (no setter): a
    value is overridden by inserting the `system_settings` row and invalidating
    the 60-second cache, then deleted in `finally` — exactly the shape
    `tests/modules/notifications/test_channels.py` uses for its own kill switch.
    Scoped to one key, never a blanket DELETE (the test database is shared)."""
    db.add(SystemSetting(key="norms_publish_scope", value="leshoz"))
    await db.commit()
    settings_store.invalidate("norms_publish_scope")
    try:
        created = await _draft(
            gis_specialist_client,
            published_contour.id,
            grazing_activity_id,
            geobotanic_doc_id=str(survey_doc.id),
        )
        norm_id = created.json()["id"]
        await gis_specialist_client.post(f"/api/v1/norms/{norm_id}/submit-review")
        await leadership_client.post(
            f"/api/v1/norms/{norm_id}/approve", json={"approval_doc_id": str(approval_doc.id)}
        )
        published = await leadership_client.post(f"/api/v1/norms/{norm_id}/publish")
        assert published.status_code == 200
        assert published.json()["status"] == "published"
    finally:
        await db.execute(text("DELETE FROM system_settings WHERE key = 'norms_publish_scope'"))
        await db.commit()
        settings_store.invalidate("norms_publish_scope")


async def test_submitting_for_review_without_the_survey_file_is_refused(
    gis_specialist_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
) -> None:
    """VMQ 689: the norm comes out of a geobotanical survey. A norm with no survey
    file is somebody's guess, and must not reach a reviewer."""
    created = await _draft(gis_specialist_client, published_contour.id, grazing_activity_id)
    response = await gis_specialist_client.post(
        f"/api/v1/norms/{created.json()['id']}/submit-review"
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "geobotanic_doc_required"


async def test_a_second_published_norm_for_the_same_period_is_a_409(
    gis_specialist_client: AsyncClient,
    leadership_client: AsyncClient,
    central_admin_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    survey_doc: MediaFile,
    approval_doc: MediaFile,
) -> None:
    # A fixed 2-tuple, not `range(2)`: pyright's definite-assignment analysis
    # only recognizes a literal-tuple `for` loop as guaranteed non-empty (same
    # shape as test_parameters_api.py's own
    # test_publishing_an_overlapping_period_is_a_409), so `response` below
    # would otherwise be "possibly unbound" — confirmed empirically.
    for _ in (0, 1):
        created = await _draft(
            gis_specialist_client,
            published_contour.id,
            grazing_activity_id,
            geobotanic_doc_id=str(survey_doc.id),
        )
        norm_id = created.json()["id"]
        await gis_specialist_client.post(f"/api/v1/norms/{norm_id}/submit-review")
        await leadership_client.post(
            f"/api/v1/norms/{norm_id}/approve", json={"approval_doc_id": str(approval_doc.id)}
        )
        response = await central_admin_client.post(f"/api/v1/norms/{norm_id}/publish")
    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "period_overlap"


async def test_publishing_a_norm_for_a_contour_with_no_published_version_is_refused(
    gis_specialist_client: AsyncClient,
    leadership_client: AsyncClient,
    central_admin_client: AsyncClient,
    draft_only_contour: Contour,
    grazing_activity_id: uuid.UUID,
    survey_doc: MediaFile,
    approval_doc: MediaFile,
) -> None:
    """Ruling 17: MaxSB is computed from the published version's `area_ha`. With
    no published version there is no area of record, and a norm published anyway
    would carry a MaxSB of nothing."""
    created = await _draft(
        gis_specialist_client,
        draft_only_contour.id,
        grazing_activity_id,
        geobotanic_doc_id=str(survey_doc.id),
    )
    norm_id = created.json()["id"]
    await gis_specialist_client.post(f"/api/v1/norms/{norm_id}/submit-review")
    await leadership_client.post(
        f"/api/v1/norms/{norm_id}/approve", json={"approval_doc_id": str(approval_doc.id)}
    )
    response = await central_admin_client.post(f"/api/v1/norms/{norm_id}/publish")
    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "no_published_contour"


async def test_a_norm_outside_the_actors_zone_is_refused(
    other_zone_specialist_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
) -> None:
    """Zone scoping is not a permission check (lesson) — a GIS specialist of
    another leshoz holds `norms.manage` and must still be refused."""
    created = await _draft(other_zone_specialist_client, published_contour.id, grazing_activity_id)
    assert created.status_code == 403
    assert created.json()["error"]["code"] == "ERR-ACL-002"


async def test_publishing_a_grazing_norm_without_a_yield_is_refused(
    gis_specialist_client: AsyncClient,
    leadership_client: AsyncClient,
    central_admin_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    survey_doc: MediaFile,
    approval_doc: MediaFile,
) -> None:
    """I2 (final review): `yield_c_per_ha` is nullable and optional, and
    `publish_norm` computed `max_sb` only when it was present. A grazing norm
    published without it therefore switched the VMQ 689 limit OFF — `_norm_check`
    passed (a norm exists), `_limit_check` reported `skipped`/`no_limit`, and
    `save_calculation` accepted any herd size at all, with `max_sb=None`. The one
    limit the decree exists to impose was removed by omitting an optional field,
    and the check report said `skipped`, not `fail`."""
    created = await _draft(
        gis_specialist_client,
        published_contour.id,
        grazing_activity_id,
        yield_c_per_ha=None,
        geobotanic_doc_id=str(survey_doc.id),
    )
    assert created.status_code == 201, created.text
    norm_id = created.json()["id"]
    await gis_specialist_client.post(f"/api/v1/norms/{norm_id}/submit-review")
    await leadership_client.post(
        f"/api/v1/norms/{norm_id}/approve", json={"approval_doc_id": str(approval_doc.id)}
    )

    refused = await central_admin_client.post(f"/api/v1/norms/{norm_id}/publish")
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["details"]["reason"] == "yield_required"


async def test_a_norm_for_a_haymaking_contour_publishes_without_a_yield(
    gis_specialist_client: AsyncClient,
    leadership_client: AsyncClient,
    central_admin_client: AsyncClient,
    published_contour: Contour,
    haymaking_activity_id: uuid.UUID,
    survey_doc: MediaFile,
    approval_doc: MediaFile,
) -> None:
    """The other half of I2's rule: VMQ 689's feed-stock limit binds GRAZING
    and nothing else (ruling 13), so a non-grazing norm with no yield is
    legitimate and publishes with `max_sb` left NULL."""
    created = await _draft(
        gis_specialist_client,
        published_contour.id,
        haymaking_activity_id,
        yield_c_per_ha=None,
        geobotanic_doc_id=str(survey_doc.id),
    )
    norm_id = created.json()["id"]
    await gis_specialist_client.post(f"/api/v1/norms/{norm_id}/submit-review")
    await leadership_client.post(
        f"/api/v1/norms/{norm_id}/approve", json={"approval_doc_id": str(approval_doc.id)}
    )

    published = await central_admin_client.post(f"/api/v1/norms/{norm_id}/publish")
    assert published.status_code == 200, published.text
    assert published.json()["max_sb"] is None


async def test_publishing_a_norm_outside_the_actors_zone_is_refused(
    gis_specialist_client: AsyncClient,
    leadership_client: AsyncClient,
    other_zone_publisher_client: AsyncClient,
    published_contour: Contour,
    grazing_activity_id: uuid.UUID,
    survey_doc: MediaFile,
    approval_doc: MediaFile,
) -> None:
    """Deferred minor #10: zone scoping is a Global Constraint applied on all
    eight norm write paths, but only `create` had HTTP-level cover — a
    regression on any of the other seven would have been silent. `publish` is
    the highest-value edge: it is what puts a limit into force.

    The actor holds `norms.publish` and is refused with no `reason` at all,
    which is what distinguishes `_assert_norm_zone`'s refusal from
    `_assert_may_publish`'s own `central_publication_required` (ruling 16),
    which the same code would otherwise be indistinguishable from."""
    created = await _draft(
        gis_specialist_client,
        published_contour.id,
        grazing_activity_id,
        geobotanic_doc_id=str(survey_doc.id),
    )
    norm_id = created.json()["id"]
    await gis_specialist_client.post(f"/api/v1/norms/{norm_id}/submit-review")
    await leadership_client.post(
        f"/api/v1/norms/{norm_id}/approve", json={"approval_doc_id": str(approval_doc.id)}
    )

    refused = await other_zone_publisher_client.post(f"/api/v1/norms/{norm_id}/publish")
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "ERR-ACL-002"
    assert refused.json()["error"]["details"] == {}
