"""`gis.service.public_features` — the anonymous mirror of `list_features`
that 4.6 `public`'s open-data layer read uses (design/01 rule 2: `public` may
reach this module only through its service)."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.gis import service
from app.modules.gis.models import GisLayer
from tests.modules.gis.conftest import make_feature, random_box_wkt


async def _layer(db: AsyncSession, code: str) -> GisLayer:
    return (await db.execute(select(GisLayer).where(GisLayer.code == code))).scalar_one()


async def test_a_public_layers_published_features_are_readable_anonymously(db: AsyncSession):
    """`flush`, never `commit`: `service.public_features` reads through this
    same session, and a COMMITTED published `forest_fund` row outlives the
    test in the worker's shared database — from then on `gis.checks.
    _within_fund` sees a non-empty layer and every later contour placed at
    its own random spot fails `outside_forest_fund` on submit (the cross-
    module journey did, under `-n 4`, once #202's tests shifted the
    distribution). The `db` fixture's teardown rollback undoes a flush."""
    layer = await _layer(db, "forest_fund")
    published = await make_feature(db, layer, random_box_wkt(), status="published")
    await db.flush()

    collection = await service.public_features(db, "forest_fund")
    assert collection["type"] == "FeatureCollection"
    assert str(published.id) in {f["id"] for f in collection["features"]}


async def test_a_draft_feature_on_a_public_layer_is_not_returned(db: AsyncSession):
    """The shared test DB already carries published `forest_fund` features from
    other tests (lesson: never assume an empty layer) — assert the DRAFT row's
    own id is absent, not that the whole collection is empty."""
    layer = await _layer(db, "forest_fund")
    draft = await make_feature(db, layer, random_box_wkt(), status="draft")
    await db.flush()

    collection = await service.public_features(db, "forest_fund")
    assert str(draft.id) not in {f["id"] for f in collection["features"]}


async def test_a_non_public_layer_refuses_the_anonymous_read(db: AsyncSession):
    with pytest.raises(DomainError) as exc_info:
        await service.public_features(db, "restrictions")
    assert exc_info.value.code == "ERR-SYS-003"


async def test_an_unknown_layer_code_is_the_same_refusal(db: AsyncSession):
    with pytest.raises(DomainError) as exc_info:
        await service.public_features(db, "does-not-exist")
    assert exc_info.value.code == "ERR-SYS-003"
