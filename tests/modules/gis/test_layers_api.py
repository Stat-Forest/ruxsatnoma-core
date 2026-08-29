"""The catalogue is fixed (ruling 19): read is open to any authenticated user,
presentation is editable under gis.layers.manage, and there is no create/delete."""

from app.modules.gis.models import LAYER_CODES


async def test_any_authenticated_user_lists_the_15_layers(applicant_client):
    resp = await applicant_client.get("/api/v1/gis/layers")
    assert resp.status_code == 200
    codes = {item["code"] for item in resp.json()["items"]}
    assert set(LAYER_CODES) <= codes


async def test_layer_presentation_is_editable_with_the_permission(gis_client):
    resp = await gis_client.patch(
        "/api/v1/gis/layers/restrictions",
        json={"style": {"color": "#c00"}, "is_public": False},
    )
    assert resp.status_code == 200
    assert resp.json()["style"] == {"color": "#c00"}


async def test_layer_patch_is_refused_without_the_permission(applicant_client):
    resp = await applicant_client.patch(
        "/api/v1/gis/layers/restrictions",
        json={"is_public": True},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"


async def test_unknown_layer_is_404(gis_client):
    resp = await gis_client.patch("/api/v1/gis/layers/nope", json={"is_public": True})
    assert resp.status_code == 404
