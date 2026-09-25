"""`PATCH /auth/applicants/{applicant_id}/address` (ruling #113, plan 07.4 task 5a).

The route's own claim: 200 for the caller's own applicant — individual or
legal (decision #226, R4) — the SAME 404 `ERR-SYS-003` for a stranger's
applicant and for one that does not exist at all — never a 403, which would
turn the route into an applicant-existence oracle for anybody holding a
session (`service.update_applicant_address`'s own docstring).
"""

import uuid

from app.main import create_app
from app.modules.auth.models import Applicant, User
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"


def unique_pinfl() -> str:
    return f"1{uuid.uuid4().int % 10**13:013d}"


def unique_stir() -> str:
    return f"9{uuid.uuid4().int % 10**8:08d}"


async def _sign_in(db, user: User) -> tuple[str, str]:
    """Session cookie/csrf pair for `user`, committed so the app's own
    connection (a separate one from the test's `db`) can see it."""
    _, token, csrf = await make_session(db, user)
    await db.commit()
    return token, csrf


async def test_the_owner_may_set_their_own_address(db) -> None:
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    applicant = Applicant(
        kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id
    )
    db.add(applicant)
    await db.flush()
    token, csrf = await _sign_in(db, user)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        response = await client.patch(
            f"{API}/auth/applicants/{applicant.id}/address",
            json={"address": "Toshkent, Chilonzor tumani, 12-uy"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(applicant.id)
    assert body["address"] == "Toshkent, Chilonzor tumani, 12-uy"


async def test_an_organisations_own_account_may_set_its_address(db) -> None:
    """Decision #226: an organisation's cabinet is a 1:1 account exactly like
    an individual's — `owner_user_id` on a `kind='legal'` row — so this route
    needs no separate case for it beyond `own_applicant_ids` already covering
    both kinds."""
    org_user = await make_user(db, role_code="applicant", pinfl=None)
    legal = Applicant(kind="legal", stir=unique_stir(), name="OOO SELF", owner_user_id=org_user.id)
    db.add(legal)
    await db.flush()
    token, csrf = await _sign_in(db, org_user)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        response = await client.patch(
            f"{API}/auth/applicants/{legal.id}/address",
            json={"address": "Toshkent, Yunusobod tumani, 5-uy"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["address"] == "Toshkent, Yunusobod tumani, 5-uy"


async def test_a_stranger_gets_404_never_403(db) -> None:
    stranger = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    db.add(
        Applicant(
            kind="individual",
            pinfl=stranger.pinfl,
            name=stranger.full_name,
            owner_user_id=stranger.id,
        )
    )
    other = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    other_applicant = Applicant(
        kind="individual", pinfl=other.pinfl, name=other.full_name, owner_user_id=other.id
    )
    db.add(other_applicant)
    await db.flush()
    token, csrf = await _sign_in(db, stranger)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        response = await client.patch(
            f"{API}/auth/applicants/{other_applicant.id}/address",
            json={"address": "should not land"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR-SYS-003"


async def test_an_applicant_id_that_does_not_exist_is_also_404(db) -> None:
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    db.add(
        Applicant(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    )
    await db.flush()
    token, csrf = await _sign_in(db, user)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        response = await client.patch(
            f"{API}/auth/applicants/{uuid.uuid4()}/address",
            json={"address": "should not land"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_blank_address_is_rejected(db) -> None:
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    applicant = Applicant(
        kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id
    )
    db.add(applicant)
    await db.flush()
    token, csrf = await _sign_in(db, user)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        response = await client.patch(
            f"{API}/auth/applicants/{applicant.id}/address", json={"address": "   "}
        )
    assert response.status_code == 422
