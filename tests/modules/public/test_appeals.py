"""Citizen appeals: anonymous submit/check (R3's "no oracle" shape) plus staff
triage under `public.appeals.manage`."""

from app.main import create_app
from tests.conftest import make_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.public.conftest import appeals_manager, auth_client

API = "/api/v1"


async def _submit(client, *, contact: dict, subject: str = "Subject", body: str = "Body text"):
    r = await client.post(
        f"{API}/public/appeals",
        json={
            "applicant_name": "Test Citizen",
            "contact": contact,
            "subject": subject,
            "body": body,
        },
    )
    assert r.status_code == 201
    return r.json()["number"]


async def test_submit_then_check_with_the_right_contact_finds_it(db):
    async with make_client(create_app(), lifespan=True) as client:
        number = await _submit(client, contact={"phone": "+998901234567"})
        assert number.startswith("MR-")

        found = await client.get(
            f"{API}/public/appeals/check", params={"number": number, "phone": "998901234567"}
        )
    assert found.status_code == 200
    body = found.json()
    assert body["found"] is True
    assert body["status"] == "new"


async def test_check_with_the_wrong_contact_answers_not_found(db):
    async with make_client(create_app(), lifespan=True) as client:
        number = await _submit(client, contact={"phone": "+998901234567"})
        wrong = await client.get(
            f"{API}/public/appeals/check", params={"number": number, "phone": "999999999"}
        )
    assert wrong.json() == {
        "found": False,
        "status": None,
        "subject": None,
        "answer_text": None,
        "answered_at": None,
    }


async def test_an_unknown_number_answers_identically_to_a_contact_mismatch(db):
    async with make_client(create_app(), lifespan=True) as client:
        r = await client.get(
            f"{API}/public/appeals/check",
            params={"number": "MR-2026-999999", "phone": "998900000000"},
        )
    assert r.json()["found"] is False


async def test_submitting_with_neither_phone_nor_email_is_a_422(db):
    async with make_client(create_app(), lifespan=True) as client:
        r = await client.post(
            f"{API}/public/appeals",
            json={
                "applicant_name": "Test Citizen",
                "contact": {},
                "subject": "Subject",
                "body": "Body text",
            },
        )
    assert r.status_code == 422


async def test_staff_without_the_permission_is_refused(db):
    other = await make_user(db)
    _, other_token, other_csrf = await make_session(db, other)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, other_token, other_csrf)
        r = await client.get(f"{API}/admin/public/appeals")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_staff_lists_gets_advances_and_answers_an_appeal(db):
    _, token, csrf = await appeals_manager(db)
    async with make_client(create_app(), lifespan=True) as client:
        number = await _submit(
            client,
            contact={"email": "citizen@example.uz"},
            subject="Road blocked",
            body="There is a fallen tree.",
        )

        auth_client(client, token, csrf)
        listing = await client.get(f"{API}/admin/public/appeals")
        assert listing.status_code == 200
        appeal = next(item for item in listing.json()["items"] if item["number"] == number)

        advance = await client.post(
            f"{API}/admin/public/appeals/{appeal['id']}/status", json={"to_status": "in_progress"}
        )
        assert advance.status_code == 200
        assert advance.json()["status"] == "in_progress"

        answer = await client.post(
            f"{API}/admin/public/appeals/{appeal['id']}/answer",
            json={"answer_text": "Removed, thank you."},
        )
    assert answer.status_code == 200
    assert answer.json()["status"] == "answered"
    assert answer.json()["answer_text"] == "Removed, thank you."


async def test_a_new_appeal_can_be_answered_directly(db):
    _, token, csrf = await appeals_manager(db)
    async with make_client(create_app(), lifespan=True) as client:
        number = await _submit(client, contact={"email": "direct-answer@example.uz"})

        auth_client(client, token, csrf)
        listing = await client.get(f"{API}/admin/public/appeals")
        appeal = next(item for item in listing.json()["items"] if item["number"] == number)
        assert appeal["status"] == "new"

        answer = await client.post(
            f"{API}/admin/public/appeals/{appeal['id']}/answer", json={"answer_text": "Done."}
        )
    assert answer.status_code == 200
    assert answer.json()["status"] == "answered"


async def test_an_invalid_transition_is_a_409(db):
    _, token, csrf = await appeals_manager(db)
    async with make_client(create_app(), lifespan=True) as client:
        number = await _submit(client, contact={"email": "citizen2@example.uz"})

        auth_client(client, token, csrf)
        listing = await client.get(f"{API}/admin/public/appeals")
        appeal = next(item for item in listing.json()["items"] if item["number"] == number)
        # `new -> closed` is legal; a SECOND transition from `closed` is not.
        first = await client.post(
            f"{API}/admin/public/appeals/{appeal['id']}/status", json={"to_status": "closed"}
        )
        assert first.status_code == 200
        second = await client.post(
            f"{API}/admin/public/appeals/{appeal['id']}/status", json={"to_status": "in_progress"}
        )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ERR-PUB-001"
