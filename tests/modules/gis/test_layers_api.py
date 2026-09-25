"""The catalogue is fixed (ruling 19): read is open to any authenticated user,
presentation is editable under gis.layers.manage, and there is no create/delete."""

from app.core.schemas import CODE_MAX_LENGTH
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


async def test_every_layer_carries_all_three_languages(applicant_client):
    """Migration `0031`: the catalogue's `name` had only `uz_cyrl` and `ru`.

    Uzbek Latin is the base language (decision #18) and the public site renders
    in it, so the open-data page listed its layers in Cyrillic — the only Uzbek
    the data had. Nothing was missing or empty by the payload's own lights, which
    is why no existing test saw it: the key the Latin page asks for was simply
    never seeded. This pins all three keys on every layer, so a layer added
    without a Latin name fails here instead of on the page.
    """
    resp = await applicant_client.get("/api/v1/gis/layers")
    assert resp.status_code == 200
    missing = {
        item["code"]: sorted({"uz_latn", "uz_cyrl", "ru"} - set(item["name"]))
        for item in resp.json()["items"]
        if not {"uz_latn", "uz_cyrl", "ru"} <= set(item["name"])
    }
    assert missing == {}


async def test_layer_names_in_latin_use_no_cyrillic(applicant_client):
    """The `uz_latn` value must actually be Latin.

    A copy of the Cyrillic string into the Latin key would satisfy the test above
    while leaving the page exactly as broken, so assert on the script itself.
    """
    resp = await applicant_client.get("/api/v1/gis/layers")
    cyrillic = {
        item["code"]: item["name"]["uz_latn"]
        for item in resp.json()["items"]
        if any("Ѐ" <= ch <= "ӿ" for ch in item["name"].get("uz_latn", ""))
    }
    assert cyrillic == {}


async def test_layer_code_longer_than_the_cap_is_refused(applicant_client):
    """Stage 19: the path `{code}` is bounded at `CODE_MAX_LENGTH`. One past it
    is a 422 naming the limit; exactly at the cap is not refused for LENGTH —
    the route may still answer its own 404 for an unknown layer (ruling: not a
    specific status, just != 422 / no `string_too_long`)."""
    too_long = await applicant_client.get(f"/api/v1/gis/layers/{'a' * 65}/features")
    assert too_long.status_code == 422, too_long.text
    error = too_long.json()["error"]["details"]["errors"][0]
    assert error["type"] == "string_too_long"
    assert error["ctx"]["max_length"] == CODE_MAX_LENGTH

    at_cap = await applicant_client.get(f"/api/v1/gis/layers/{'a' * CODE_MAX_LENGTH}/features")
    assert at_cap.status_code != 422, at_cap.text
