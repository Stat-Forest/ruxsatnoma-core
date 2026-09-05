"""FAQ: anonymous read of `published` items only; `help.faq.manage` CRUD."""

from app.main import create_app
from tests.conftest import make_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.help.conftest import auth_client, faq_manager

API = "/api/v1"


async def test_public_faq_lists_only_published_items(db):
    _, token, csrf = await faq_manager(db)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(
            f"{API}/admin/help/faq",
            json={
                "question": {"uz_cyrl": "Савол?"},
                "answer": {"uz_cyrl": "Жавоб."},
                "sort_order": 1,
            },
        )
        assert created.status_code == 201
        faq_id = created.json()["id"]

        client.cookies.clear()
        client.headers.pop("X-CSRF-Token", None)
        still_draft = await client.get(f"{API}/help/faq")
        assert faq_id not in {row["id"] for row in still_draft.json()}

        auth_client(client, token, csrf)
        published = await client.patch(
            f"{API}/admin/help/faq/{faq_id}", json={"status": "published"}
        )
        assert published.status_code == 200

        client.cookies.clear()
        client.headers.pop("X-CSRF-Token", None)
        now_visible = await client.get(f"{API}/help/faq")
    assert faq_id in {row["id"] for row in now_visible.json()}


async def test_faq_write_requires_a_session(db):
    async with make_client(create_app(), lifespan=True) as client:
        r = await client.post(
            f"{API}/admin/help/faq",
            json={"question": {"uz_cyrl": "Q"}, "answer": {"uz_cyrl": "A"}},
        )
    assert r.status_code == 401


async def test_faq_write_requires_the_permission(db):
    other = await make_user(db)
    _, token, csrf = await make_session(db, other)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/help/faq",
            json={"question": {"uz_cyrl": "Q"}, "answer": {"uz_cyrl": "A"}},
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"
