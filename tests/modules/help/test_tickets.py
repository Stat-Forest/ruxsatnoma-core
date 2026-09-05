"""Support tickets: any authenticated user opens one; `help.tickets.manage`
assigns/resolves; a closed ticket refuses a new message."""

from app.main import create_app
from tests.conftest import make_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.help.conftest import auth_client, tickets_manager

API = "/api/v1"


async def _signed_in(db, **overrides):
    user = await make_user(db, **overrides)
    _, token, csrf = await make_session(db, user)
    await db.commit()
    return user, token, csrf


async def test_a_user_opens_and_reads_their_own_ticket(db):
    user, token, csrf = await _signed_in(db)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(
            f"{API}/help/tickets", json={"subject": "Cannot log in", "body": "Help please"}
        )
        assert created.status_code == 201
        ticket_id = created.json()["id"]
        assert created.json()["number"].startswith("ST-")

        got = await client.get(f"{API}/help/tickets/{ticket_id}")
    assert got.status_code == 200
    assert len(got.json()["messages"]) == 1
    assert got.json()["messages"][0]["body"] == "Help please"


async def test_a_stranger_cannot_read_someone_elses_ticket(db):
    owner, owner_token, owner_csrf = await _signed_in(db)
    stranger, stranger_token, stranger_csrf = await _signed_in(db)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, owner_token, owner_csrf)
        created = await client.post(
            f"{API}/help/tickets", json={"subject": "Private", "body": "Secret"}
        )
        ticket_id = created.json()["id"]

        auth_client(client, stranger_token, stranger_csrf)
        r = await client.get(f"{API}/help/tickets/{ticket_id}")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_manage_can_assign_and_resolve_but_a_stranger_cannot(db):
    owner, owner_token, owner_csrf = await _signed_in(db)
    manager, manager_token, manager_csrf = await tickets_manager(db)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, owner_token, owner_csrf)
        created = await client.post(
            f"{API}/help/tickets", json={"subject": "Billing question", "body": "Why?"}
        )
        ticket_id = created.json()["id"]

        auth_client(client, manager_token, manager_csrf)
        assign = await client.post(
            f"{API}/help/tickets/{ticket_id}/assign", json={"assignee_id": str(manager.id)}
        )
        assert assign.status_code == 200
        assert assign.json()["status"] == "in_progress"

        resolve = await client.post(f"{API}/help/tickets/{ticket_id}/resolve")
        assert resolve.status_code == 200
        assert resolve.json()["status"] == "resolved"

        auth_client(client, owner_token, owner_csrf)
        forbidden_assign = await client.post(
            f"{API}/help/tickets/{ticket_id}/assign", json={"assignee_id": str(owner.id)}
        )
    assert forbidden_assign.status_code == 403


async def test_a_closed_ticket_refuses_a_new_message(db):
    owner, owner_token, owner_csrf = await _signed_in(db)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, owner_token, owner_csrf)
        created = await client.post(
            f"{API}/help/tickets", json={"subject": "Done already", "body": "Thanks"}
        )
        ticket_id = created.json()["id"]

        closed = await client.post(f"{API}/help/tickets/{ticket_id}/close")
        assert closed.status_code == 200
        assert closed.json()["closed_at"] is not None

        r = await client.post(
            f"{API}/help/tickets/{ticket_id}/messages", json={"body": "Are you still there?"}
        )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ERR-HELP-001"


async def test_my_tickets_lists_only_my_own_unless_i_manage(db):
    mine, mine_token, mine_csrf = await _signed_in(db)
    other, other_token, other_csrf = await _signed_in(db)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, mine_token, mine_csrf)
        await client.post(f"{API}/help/tickets", json={"subject": "Mine", "body": "b"})

        auth_client(client, other_token, other_csrf)
        await client.post(f"{API}/help/tickets", json={"subject": "Other", "body": "b"})

        auth_client(client, mine_token, mine_csrf)
        listing = await client.get(f"{API}/help/tickets")
    subjects = {row["subject"] for row in listing.json()["items"]}
    assert "Mine" in subjects
    assert "Other" not in subjects
