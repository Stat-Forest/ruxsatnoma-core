"""ruling 6 (3.3b): a poa representation requires a real, own, active PDF file."""

import uuid
from datetime import date, timedelta

from app.core.time import business_today
from app.main import create_app
from app.modules.auth.models import Applicant, Representation, User
from tests.conftest import make_client
from tests.core.test_files_api import PDF, PNG, registered_applicant, upload
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session

API = "/api/v1"


def unique_stir() -> str:
    # Leading digit 8: 9 is already claimed by tests/modules/auth/test_legal_applicants.py
    # sharing this same persistent test DB.
    return f"8{uuid.uuid4().int % 10**8:08d}"


def poa_payload(file_id: str, stir: str) -> dict:
    return {
        "stir": stir,
        "name": "Тест ЮЛ",
        "basis": "poa",
        "poa_file_id": file_id,
        "valid_until": str(date.today() + timedelta(days=30)),
    }


async def _director_with_applicant(db) -> tuple[User, Applicant]:
    """A user with an already-effective org_eri representation for a fresh legal
    applicant, built directly (no eimzo mock needed) — add_representation's poa
    branch requires the caller to already hold org_eri/director_registry.
    registered_applicant (not raw make_user) so the account clears the
    registration gate (ERR-AUTH-008) needed to call POST /files."""
    director = await registered_applicant(db)
    applicant = Applicant(kind="legal", stir=unique_stir(), name="OOO DIRECTOR")
    db.add(applicant)
    await db.flush()
    db.add(
        Representation(
            applicant_id=applicant.id,
            user_id=director.id,
            basis="org_eri",
            valid_from=business_today(),
        )
    )
    await db.flush()
    return director, applicant


async def test_poa_with_own_uploaded_pdf_succeeds(db):
    user = await registered_applicant(db)
    _, token, csrf = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        file_id = (await upload(client, PDF)).json()["id"]
        r = await client.post(f"{API}/auth/applicants", json=poa_payload(file_id, unique_stir()))
        assert r.status_code == 201, r.text
        assert r.json()["representation"]["basis"] == "poa"


async def test_poa_with_unknown_file_id_rejected(db):
    user = await registered_applicant(db)
    _, token, csrf = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/auth/applicants", json=poa_payload(str(uuid.uuid4()), unique_stir())
        )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "ERR-VAL-001"
    assert body["error"]["details"]["reason"] == "poa_file_not_found"


async def test_poa_with_someone_elses_file_rejected(db):
    owner = await registered_applicant(db)
    _, owner_token, owner_csrf = await make_session(db, owner)
    stranger = await registered_applicant(db)
    _, str_token, str_csrf = await make_session(db, stranger)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, owner_token, owner_csrf)
        file_id = (await upload(client, PDF)).json()["id"]
    async with make_client(app, lifespan=True) as client:
        auth_client(client, str_token, str_csrf)
        r = await client.post(f"{API}/auth/applicants", json=poa_payload(file_id, unique_stir()))
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "poa_file_not_owned"


async def test_poa_with_non_pdf_rejected(db):
    user = await registered_applicant(db)
    _, token, csrf = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        file_id = (await upload(client, PNG, filename="pic.png", content_type="image/png")).json()[
            "id"
        ]
        r = await client.post(f"{API}/auth/applicants", json=poa_payload(file_id, unique_stir()))
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "poa_file_not_pdf"


async def test_add_representation_poa_with_own_uploaded_pdf_succeeds(db):
    director, applicant = await _director_with_applicant(db)
    candidate = await registered_applicant(db)
    _, token, csrf = await make_session(db, director)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        file_id = (await upload(client, PDF)).json()["id"]
        r = await client.post(
            f"{API}/auth/applicants/{applicant.id}/representations",
            json={
                "user_pinfl": candidate.pinfl,
                "basis": "poa",
                "poa_file_id": file_id,
                "valid_until": str(date.today() + timedelta(days=30)),
            },
        )
    assert r.status_code == 201, r.text
    assert r.json()["basis"] == "poa"


async def test_add_representation_poa_with_unknown_file_id_rejected(db):
    director, applicant = await _director_with_applicant(db)
    candidate = await registered_applicant(db)
    _, token, csrf = await make_session(db, director)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/auth/applicants/{applicant.id}/representations",
            json={
                "user_pinfl": candidate.pinfl,
                "basis": "poa",
                "poa_file_id": str(uuid.uuid4()),
                "valid_until": str(date.today() + timedelta(days=30)),
            },
        )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "poa_file_not_found"
