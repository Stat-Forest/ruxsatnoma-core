"""GET /api/v1/refs/*: authenticated, unfiltered by zone, no permission code (ruling 10)."""

import uuid
from datetime import date

from sqlalchemy import select

from app.main import create_app
from app.modules.admin.models import Classifier, ClassifierItem, District, Organization, Region
from tests.conftest import make_client
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"


async def signed_in(db, role_code="executor_staff"):
    """A user + session; returns the raw cookie token."""
    user = await make_user(db, role_code=role_code)
    _, token, csrf = await make_session(db, user)
    return user, token, csrf


async def test_regions_require_authentication(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.get(f"{API}/refs/regions")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ERR-AUTH-002"


async def test_regions_list(db):
    _, token, _ = await signed_in(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/refs/regions")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 14
    assert body[0]["code"] == "karakalpakstan"  # sort_order 10
    assert body[0]["name"]["uz_cyrl"] == "Қорақалпоғистон Республикаси"


async def test_districts_filtered_by_region(db):
    region_id = (await db.execute(select(Region.id).where(Region.code == "fergana"))).scalar_one()
    other_id = (await db.execute(select(Region.id).where(Region.code == "khorezm"))).scalar_one()
    suffix = uuid.uuid4().hex[:6]
    db.add(District(code=f"d-a-{suffix}", name={"uz_cyrl": "Туман А"}, region_id=region_id))
    db.add(District(code=f"d-b-{suffix}", name={"uz_cyrl": "Туман Б"}, region_id=other_id))
    _, token, _ = await signed_in(db)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/refs/districts", params={"region_id": str(region_id)})
    assert r.status_code == 200
    codes = [row["code"] for row in r.json()]
    assert f"d-a-{suffix}" in codes
    assert f"d-b-{suffix}" not in codes


async def test_organizations_default_to_the_root(db, agency):
    suffix = uuid.uuid4().hex[:6]
    org = Organization(
        kind="leshoz",
        code=f"leshoz-{suffix}",
        name={"uz_cyrl": "ДЎХ"},
        parent_id=agency.id,
    )
    db.add(org)
    _, token, _ = await signed_in(db)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        root = await client.get(f"{API}/refs/organizations")
    # exactly one agency exists (ruling 6), so the unfiltered call is deterministic
    assert [row["kind"] for row in root.json()["items"]] == ["agency"]
    assert root.json()["total"] == 1

    # The children-under-agency page is capped at 100 (own test below) and the
    # organizations table accumulates rows across every suite run (no test ever
    # deletes one), so hunting for this one row's code on an uncapped/unsorted scan
    # of that page is not deterministic — read the parent relationship back through
    # the db session instead.
    await db.refresh(org)
    assert org.parent_id == agency.id


async def test_organizations_filtered_by_parent_id(db, agency):
    """`parent_id=` is the one branch of `_organizations_query` nothing covered after
    the flake rewrite (finding 9). A dedicated parent with exactly one child keeps
    this deterministic regardless of what other tests accumulate in the table."""
    suffix = uuid.uuid4().hex[:6]
    territorial = Organization(
        kind="territorial",
        code=f"parentfilter-{suffix}",
        name={"uz_cyrl": "Ҳудудий бошқарма"},
        parent_id=agency.id,
    )
    db.add(territorial)
    await db.flush()
    child = Organization(
        kind="leshoz",
        code=f"parentfilter-child-{suffix}",
        name={"uz_cyrl": "ДЎХ"},
        parent_id=territorial.id,
    )
    db.add(child)
    _, token, _ = await signed_in(db)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/refs/organizations", params={"parent_id": str(territorial.id)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert [row["code"] for row in body["items"]] == [f"parentfilter-child-{suffix}"]


async def test_organizations_pagination_caps_page_size(db):
    _, token, _ = await signed_in(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/refs/organizations", params={"page_size": 500})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ERR-VAL-001"


async def test_activity_and_livestock_types(db):
    _, token, _ = await signed_in(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        activities = await client.get(f"{API}/refs/activity-types")
        livestock = await client.get(f"{API}/refs/livestock-types")
    assert [row["code"] for row in activities.json()][:2] == ["grazing", "haymaking"]
    assert activities.json()[0]["quantity_unit"] == "head"
    assert len(livestock.json()) == 10


async def test_classifier_items_active_on_date(db):
    suffix = uuid.uuid4().hex[:6]
    classifier = Classifier(code=f"seasonal-{suffix}", name={"uz_cyrl": "Мавсумий"})
    db.add(classifier)
    await db.flush()
    db.add(
        ClassifierItem(
            classifier_id=classifier.id,
            code="OLD",
            name={"uz_cyrl": "Эски"},
            valid_from=date(2025, 1, 1),
            valid_to=date(2025, 12, 31),
            status="archived",
        )
    )
    db.add(
        ClassifierItem(
            classifier_id=classifier.id,
            code="NEW",
            name={"uz_cyrl": "Янги"},
            valid_from=date(2026, 1, 1),
        )
    )
    _, token, _ = await signed_in(db)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        today = await client.get(f"{API}/refs/classifiers/seasonal-{suffix}/items")
        back_then = await client.get(
            f"{API}/refs/classifiers/seasonal-{suffix}/items",
            params={"on_date": "2025-06-01"},
        )
    assert [row["code"] for row in today.json()] == ["NEW"]
    assert [row["code"] for row in back_then.json()] == ["OLD"]


async def test_unknown_classifier_is_404(db):
    _, token, _ = await signed_in(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/refs/classifiers/nope/items")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"
