"""applicants/representations/user_consents DDL: CHECKs, uniques, deferred poa FK."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.modules.auth.models import Applicant, OtpCode, Representation, Role, User, UserConsent


async def make_user(db, *, role_code="applicant", pinfl=None) -> User:
    role_id = (await db.execute(select(Role.id).where(Role.code == role_code))).scalar_one()
    user = User(full_name="T", role_id=role_id, pinfl=pinfl)
    db.add(user)
    await db.flush()
    return user


def individual(user, **overrides) -> Applicant:
    defaults = dict(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    return Applicant(**{**defaults, **overrides})


async def test_individual_applicant_roundtrip(db):
    user = await make_user(db, pinfl="11111111111111")
    row = individual(user)
    db.add(row)
    await db.flush()
    db.expunge_all()
    got = await db.get(Applicant, row.id)
    assert got is not None and got.kind == "individual" and got.verified_at is None


async def test_identity_by_kind_check(db):
    user = await make_user(db, pinfl="11111111111112")
    db.add(individual(user, stir="123456789"))  # individual must NOT carry stir
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_legal_needs_stir_not_pinfl(db):
    db.add(Applicant(kind="legal", pinfl="11111111111113", name="OOO X"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_stir_format_ascii_only(db):
    db.add(Applicant(kind="legal", stir="12345678٩", name="OOO X"))  # arabic-indic digit
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_owner_user_unique(db):
    user = await make_user(db, pinfl="11111111111114")
    db.add(individual(user))
    await db.flush()
    db.add(individual(user, pinfl="11111111111114"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_poa_requires_file_and_term(db):
    user = await make_user(db, pinfl="11111111111115")
    legal = Applicant(kind="legal", stir="987654321", name="OOO Y")
    db.add(legal)
    await db.flush()
    db.add(
        Representation(
            applicant_id=legal.id,
            user_id=user.id,
            basis="poa",
            valid_from=datetime.now(UTC).date(),
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_one_active_representation_per_pair(db):
    user = await make_user(db, pinfl="11111111111116")
    legal = Applicant(kind="legal", stir="987654322", name="OOO Z")
    db.add(legal)
    await db.flush()
    today = datetime.now(UTC).date()
    db.add(
        Representation(applicant_id=legal.id, user_id=user.id, basis="org_eri", valid_from=today)
    )
    await db.flush()
    db.add(
        Representation(
            applicant_id=legal.id,
            user_id=user.id,
            basis="director_registry",
            valid_from=today,
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_consent_unique_per_version(db):
    user = await make_user(db, pinfl="11111111111117")
    db.add(UserConsent(user_id=user.id, doc_type="privacy_policy", doc_version="1.0"))
    await db.flush()
    db.add(UserConsent(user_id=user.id, doc_type="privacy_policy", doc_version="1.0"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_otp_purpose_extended_and_created_at(db):
    row = OtpCode(
        code_hash="x" * 64,
        purpose="eimzo_challenge",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add(row)
    await db.flush()
    assert row.created_at is not None


async def test_user_oneid_profile_jsonb(db):
    user = await make_user(db, pinfl="11111111111118")
    user.oneid_profile = {"pinfl": "11111111111118", "legal_info": [{"le_tin": "123456789"}]}
    await db.flush()
    db.expunge_all()
    got = await db.get(User, user.id)
    assert got is not None and got.oneid_profile is not None
    assert got.oneid_profile["legal_info"][0]["le_tin"] == "123456789"
