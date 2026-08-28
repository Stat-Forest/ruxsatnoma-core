"""Legal attach (3 bases), extra representatives, PATCH /auth/me contacts."""

import uuid
from datetime import date, timedelta

from app.main import create_app
from app.modules.auth.adapters.eimzo import EimzoIdentity, encode_mock_signed_challenge
from app.modules.auth.adapters.oneid import OneIdLegalInfo, OneIdProfile, encode_mock_code
from tests.conftest import make_client
from tests.modules.auth.test_otp import last_code, unique_email, unique_phone

API = "/api/v1"


def unique_pinfl() -> str:
    return f"6{uuid.uuid4().int % 10**13:013d}"


def unique_stir() -> str:
    return f"9{uuid.uuid4().int % 10**8:08d}"


def csrf_headers(client) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token")}


async def register_individual(client, pinfl: str, *, legal_info=()) -> None:
    """OneID login + complete registration; unique phone derived from pinfl."""
    await client.get(f"{API}/auth/oneid/authorize")
    state = client.cookies.get("oneid_state")
    prof = OneIdProfile(pinfl=pinfl, full_name=f"USER {pinfl[-4:]}", legal_info=tuple(legal_info))
    r = await client.get(
        f"{API}/auth/oneid/callback", params={"code": encode_mock_code(prof), "state": state}
    )
    assert r.status_code == 200
    phone = f"+9989{pinfl[-8:]}"
    await client.post(
        f"{API}/auth/otp/request",
        json={"target_type": "phone", "target": phone, "purpose": "phone_verify"},
    )
    v = await client.post(
        f"{API}/auth/otp/verify",
        json={"target": phone, "code": last_code(), "purpose": "phone_verify"},
    )
    r = await client.post(
        f"{API}/auth/complete-registration",
        json={
            "consents": {"privacy_policy": "1.0", "offer": "1.0"},
            "phone": phone,
            "otp_token": v.json()["otp_token"],
        },
        headers=csrf_headers(client),
    )
    assert r.status_code == 200


async def org_eri_challenge(client, *, pinfl: str, stir: str) -> str:
    r = await client.post(f"{API}/auth/eimzo/challenge")
    identity = EimzoIdentity(
        challenge=r.json()["challenge"],
        pinfl=pinfl,
        full_name="DIRECTOR",
        tin=stir,
        legal_name="OOO ORG",
    )
    return encode_mock_signed_challenge(identity)


async def test_attach_via_org_eri(db):
    pinfl, stir = unique_pinfl(), unique_stir()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await register_individual(client, pinfl)
        signed = await org_eri_challenge(client, pinfl=pinfl, stir=stir)
        r = await client.post(
            f"{API}/auth/applicants",
            json={"stir": stir, "basis": "org_eri", "signed_challenge": signed},
            headers=csrf_headers(client),
        )
        assert r.status_code == 201
        body = r.json()
        assert body["applicant"]["kind"] == "legal" and body["applicant"]["stir"] == stir
        assert body["representation"]["basis"] == "org_eri"
        me = await client.get(f"{API}/auth/me")
        assert me.json()["representations"][0]["applicant"]["stir"] == stir


async def test_org_eri_tin_mismatch_403(db):
    pinfl = unique_pinfl()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await register_individual(client, pinfl)
        signed = await org_eri_challenge(client, pinfl=pinfl, stir=unique_stir())
        r = await client.post(
            f"{API}/auth/applicants",
            json={"stir": unique_stir(), "basis": "org_eri", "signed_challenge": signed},
            headers=csrf_headers(client),
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_attach_via_director_registry(db):
    pinfl, stir = unique_pinfl(), unique_stir()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await register_individual(
            client,
            pinfl,
            legal_info=[OneIdLegalInfo(le_tin=stir, le_name="OOO DIR", is_basic=True)],
        )
        r = await client.post(
            f"{API}/auth/applicants",
            json={"stir": stir, "basis": "director_registry"},
            headers=csrf_headers(client),
        )
        assert r.status_code == 201
        assert r.json()["applicant"]["name"] == "OOO DIR"


async def test_director_registry_not_listed_403(db):
    pinfl = unique_pinfl()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await register_individual(client, pinfl)
        r = await client.post(
            f"{API}/auth/applicants",
            json={"stir": unique_stir(), "basis": "director_registry"},
            headers=csrf_headers(client),
        )
    assert r.status_code == 403


async def test_attach_via_poa_requires_fields(db):
    pinfl, stir = unique_pinfl(), unique_stir()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await register_individual(client, pinfl)
        incomplete = await client.post(
            f"{API}/auth/applicants",
            json={"stir": stir, "basis": "poa"},
            headers=csrf_headers(client),
        )
        assert incomplete.status_code == 422
        r = await client.post(
            f"{API}/auth/applicants",
            json={
                "stir": stir,
                "basis": "poa",
                "poa_file_id": str(uuid.uuid4()),
                "valid_until": str(date.today() + timedelta(days=30)),
                "name": "OOO POA",
            },
            headers=csrf_headers(client),
        )
        assert r.status_code == 201
        assert r.json()["representation"]["basis"] == "poa"


async def test_duplicate_active_representation_409(db):
    pinfl, stir = unique_pinfl(), unique_stir()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await register_individual(client, pinfl)
        signed = await org_eri_challenge(client, pinfl=pinfl, stir=stir)
        assert (
            await client.post(
                f"{API}/auth/applicants",
                json={"stir": stir, "basis": "org_eri", "signed_challenge": signed},
                headers=csrf_headers(client),
            )
        ).status_code == 201
        signed2 = await org_eri_challenge(client, pinfl=pinfl, stir=stir)
        r = await client.post(
            f"{API}/auth/applicants",
            json={"stir": stir, "basis": "org_eri", "signed_challenge": signed2},
            headers=csrf_headers(client),
        )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ERR-AUTH-011"


async def test_add_second_representative(db):
    director, second, stir = unique_pinfl(), unique_pinfl(), unique_stir()
    app = create_app()
    async with make_client(app, lifespan=True) as second_client:
        await register_individual(second_client, second)
    async with make_client(app, lifespan=True) as client:
        await register_individual(client, director)
        signed = await org_eri_challenge(client, pinfl=director, stir=stir)
        attach = await client.post(
            f"{API}/auth/applicants",
            json={"stir": stir, "basis": "org_eri", "signed_challenge": signed},
            headers=csrf_headers(client),
        )
        applicant_id = attach.json()["applicant"]["id"]
        signed2 = await org_eri_challenge(client, pinfl=director, stir=stir)
        r = await client.post(
            f"{API}/auth/applicants/{applicant_id}/representations",
            json={"user_pinfl": second, "basis": "org_eri", "signed_challenge": signed2},
            headers=csrf_headers(client),
        )
        assert r.status_code == 201
    async with make_client(app, lifespan=True) as second_client:
        await second_client.get(f"{API}/auth/oneid/authorize")
        state = second_client.cookies.get("oneid_state")
        prof = OneIdProfile(pinfl=second, full_name="SECOND")
        me = await second_client.get(
            f"{API}/auth/oneid/callback", params={"code": encode_mock_code(prof), "state": state}
        )
        assert me.json()["representations"][0]["applicant"]["stir"] == stir


async def test_add_representative_requires_signed_in_candidate(db):
    director, stir = unique_pinfl(), unique_stir()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await register_individual(client, director)
        signed = await org_eri_challenge(client, pinfl=director, stir=stir)
        attach = await client.post(
            f"{API}/auth/applicants",
            json={"stir": stir, "basis": "org_eri", "signed_challenge": signed},
            headers=csrf_headers(client),
        )
        applicant_id = attach.json()["applicant"]["id"]
        signed2 = await org_eri_challenge(client, pinfl=director, stir=stir)
        r = await client.post(
            f"{API}/auth/applicants/{applicant_id}/representations",
            json={"user_pinfl": unique_pinfl(), "basis": "org_eri", "signed_challenge": signed2},
            headers=csrf_headers(client),
        )
    assert r.status_code == 404


async def test_poa_holder_cannot_add_people(db):
    holder, stir = unique_pinfl(), unique_stir()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await register_individual(client, holder)
        r = await client.post(
            f"{API}/auth/applicants",
            json={
                "stir": stir,
                "basis": "poa",
                "poa_file_id": str(uuid.uuid4()),
                "valid_until": str(date.today() + timedelta(days=30)),
                "name": "OOO POA2",
            },
            headers=csrf_headers(client),
        )
        applicant_id = r.json()["applicant"]["id"]
        add = await client.post(
            f"{API}/auth/applicants/{applicant_id}/representations",
            json={
                "user_pinfl": holder,
                "basis": "poa",
                "poa_file_id": str(uuid.uuid4()),
                "valid_until": str(date.today() + timedelta(days=30)),
            },
            headers=csrf_headers(client),
        )
    assert add.status_code == 403


async def test_patch_me_phone(db):
    pinfl, new_phone = unique_pinfl(), unique_phone()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await register_individual(client, pinfl)
        await client.post(
            f"{API}/auth/otp/request",
            json={"target_type": "phone", "target": new_phone, "purpose": "phone_verify"},
        )
        v = await client.post(
            f"{API}/auth/otp/verify",
            json={"target": new_phone, "code": last_code(), "purpose": "phone_verify"},
        )
        r = await client.patch(
            f"{API}/auth/me",
            json={"phone": new_phone, "otp_token": v.json()["otp_token"]},
            headers=csrf_headers(client),
        )
        assert r.status_code == 200
        assert r.json()["user"]["phone"] == new_phone
        assert r.json()["applicant"]["phone"] == new_phone


async def test_patch_me_email_and_exclusivity(db):
    pinfl, email = unique_pinfl(), unique_email()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await register_individual(client, pinfl)
        both = await client.patch(
            f"{API}/auth/me",
            json={"phone": unique_phone(), "email": email, "otp_token": "x"},
            headers=csrf_headers(client),
        )
        assert both.status_code == 422
        await client.post(
            f"{API}/auth/otp/request",
            json={"target_type": "email", "target": email, "purpose": "email_verify"},
        )
        v = await client.post(
            f"{API}/auth/otp/verify",
            json={"target": email, "code": last_code(), "purpose": "email_verify"},
        )
        r = await client.patch(
            f"{API}/auth/me",
            json={"email": email, "otp_token": v.json()["otp_token"]},
            headers=csrf_headers(client),
        )
        assert r.status_code == 200
        assert r.json()["user"]["email"] == email
