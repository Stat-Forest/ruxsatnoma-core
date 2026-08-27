"""Classifier administration: versioning by archive+insert (ruling 7), never a rename."""

import uuid
from datetime import date

from sqlalchemy import select

from app.main import create_app
from app.modules.admin.models import Classifier, ClassifierItem
from app.modules.admin.permissions import CLASSIFIERS_MANAGE
from app.modules.audit.models import AuditLog
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with

API = "/api/v1"


async def test_create_classifier_and_item(db):
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, CLASSIFIERS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(
            f"{API}/admin/classifiers",
            json={"code": f"cargo-{suffix}", "name": {"uz_cyrl": "Юк турлари"}},
        )
        assert created.status_code == 201, created.text
        item = await client.post(
            f"{API}/admin/classifiers/cargo-{suffix}/items",
            json={
                "code": "C-01",
                "name": {"uz_cyrl": "Биринчи"},
                "props": {"required": True},
                "valid_from": "2026-01-01",
            },
        )
        assert item.status_code == 201, item.text
        listed = await client.get(f"{API}/refs/classifiers/cargo-{suffix}/items")
    assert [row["code"] for row in listed.json()] == ["C-01"]
    assert listed.json()[0]["props"] == {"required": True}


async def test_duplicate_active_item_code_rejected(db):
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, CLASSIFIERS_MANAGE)
    classifier = Classifier(code=f"dup-{suffix}", name={"uz_cyrl": "Х"})
    db.add(classifier)
    await db.flush()
    db.add(
        ClassifierItem(
            classifier_id=classifier.id,
            code="D-01",
            name={"uz_cyrl": "Х"},
            valid_from=date(2026, 1, 1),
        )
    )
    await db.flush()
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/classifiers/dup-{suffix}/items",
            json={"code": "D-01", "name": {"uz_cyrl": "Х"}, "valid_from": "2026-02-01"},
        )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "already active"


async def test_supersede_creates_a_new_version(db):
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, CLASSIFIERS_MANAGE)
    classifier = Classifier(code=f"tar-{suffix}", name={"uz_cyrl": "Х"})
    db.add(classifier)
    await db.flush()
    old = ClassifierItem(
        classifier_id=classifier.id,
        code="T-01",
        name={"uz_cyrl": "Эски"},
        props={"modifier": "0.5"},
        valid_from=date(2026, 1, 1),
    )
    db.add(old)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/classifier-items/{old.id}/supersede",
            json={
                "code": "T-01",
                "name": {"uz_cyrl": "Янги"},
                "props": {"modifier": "0.7"},
                "valid_from": "2026-07-01",
            },
        )
        assert r.status_code == 201, r.text
        now_items = await client.get(f"{API}/refs/classifiers/tar-{suffix}/items")
        past_items = await client.get(
            f"{API}/refs/classifiers/tar-{suffix}/items", params={"on_date": "2026-03-01"}
        )

    await db.refresh(old)
    assert old.status == "archived"
    assert old.valid_to == date(2026, 6, 30)  # the day before the new version starts
    assert [row["name"]["uz_cyrl"] for row in now_items.json()] == ["Янги"]
    assert [row["name"]["uz_cyrl"] for row in past_items.json()] == ["Эски"]


async def test_supersede_requires_a_later_start(db):
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, CLASSIFIERS_MANAGE)
    classifier = Classifier(code=f"early-{suffix}", name={"uz_cyrl": "Х"})
    db.add(classifier)
    await db.flush()
    old = ClassifierItem(
        classifier_id=classifier.id,
        code="E-01",
        name={"uz_cyrl": "Эски"},
        valid_from=date(2026, 6, 1),
    )
    db.add(old)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/classifier-items/{old.id}/supersede",
            json={"code": "E-01", "name": {"uz_cyrl": "Янги"}, "valid_from": "2026-05-01"},
        )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "valid_from must be later"


async def test_archive_item_removes_it_from_refs(db):
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, CLASSIFIERS_MANAGE)
    classifier = Classifier(code=f"arch-{suffix}", name={"uz_cyrl": "Х"})
    db.add(classifier)
    await db.flush()
    item = ClassifierItem(
        classifier_id=classifier.id,
        code="A-01",
        name={"uz_cyrl": "Х"},
        valid_from=date(2026, 1, 1),
    )
    db.add(item)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        archived = await client.post(f"{API}/admin/classifier-items/{item.id}/archive")
        listed = await client.get(f"{API}/refs/classifiers/arch-{suffix}/items")
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert listed.json() == []


async def test_archived_item_with_future_valid_to_disappears_from_refs(db):
    """Finding 1: the date range alone cannot tell "pulled early" apart from "still
    current" when `valid_to` was set to a date that hasn't arrived yet — `/refs`
    must also check `status` for a "what's current" (no `on_date`) query."""
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, CLASSIFIERS_MANAGE)
    classifier = Classifier(code=f"future-{suffix}", name={"uz_cyrl": "Х"})
    db.add(classifier)
    await db.flush()
    item = ClassifierItem(
        classifier_id=classifier.id,
        code="F-01",
        name={"uz_cyrl": "Х"},
        valid_from=date(2026, 1, 1),
        valid_to=date(2030, 1, 1),  # far future: date range alone would keep it visible
    )
    db.add(item)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        archived = await client.post(f"{API}/admin/classifier-items/{item.id}/archive")
        listed = await client.get(f"{API}/refs/classifiers/future-{suffix}/items")
    assert archived.status_code == 200, archived.text
    assert listed.json() == []


async def test_archive_same_day_valid_from_does_not_500(db):
    """Finding 2: `valid_from` today (or later) plus no explicit `valid_to` must not
    make the default-end-date fallback compute something earlier than `valid_from`
    — that would fail the `valid_period` CHECK and surface as a 500, not a clean
    archive."""
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, CLASSIFIERS_MANAGE)
    classifier = Classifier(code=f"sameday-{suffix}", name={"uz_cyrl": "Х"})
    db.add(classifier)
    await db.flush()
    item = ClassifierItem(
        classifier_id=classifier.id,
        code="S-01",
        name={"uz_cyrl": "Х"},
        valid_from=date.today(),
    )
    db.add(item)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        archived = await client.post(f"{API}/admin/classifier-items/{item.id}/archive")
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"


async def test_patch_item_audits_old_and_new(db):
    suffix = uuid.uuid4().hex[:6]
    _, token, csrf = await signed_in_with(db, CLASSIFIERS_MANAGE)
    classifier = Classifier(code=f"patch-{suffix}", name={"uz_cyrl": "Х"})
    db.add(classifier)
    await db.flush()
    item = ClassifierItem(
        classifier_id=classifier.id,
        code="P-01",
        name={"uz_cyrl": "Эски"},
        valid_from=date(2026, 1, 1),
    )
    db.add(item)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.patch(
            f"{API}/admin/classifier-items/{item.id}", json={"name": {"uz_cyrl": "Янги"}}
        )
    assert r.status_code == 200
    entry = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.action == "classifier_item.update", AuditLog.object_id == item.id)
            .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert entry.old_value["name"]["uz_cyrl"] == "Эски"
    assert entry.new_value["name"]["uz_cyrl"] == "Янги"


async def test_items_of_unknown_classifier_is_404(db):
    _, token, csrf = await signed_in_with(db, CLASSIFIERS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/classifiers/no-such/items",
            json={"code": "X", "name": {"uz_cyrl": "Х"}, "valid_from": "2026-01-01"},
        )
    assert r.status_code == 404
