import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


@pytest.mark.asyncio
async def test_certificate_serial_issuer_pair_is_unique(db):
    await db.execute(
        text(
            "INSERT INTO certificates (id, user_id, serial_number, issuer, subject,"
            " pinfl_or_stir, valid_from, valid_to, status)"
            " VALUES (gen_random_uuid(), NULL, 'SER-1', 'ISS-1', 'CN=A', '12345678901',"
            " now(), now() + interval '1 year', 'active')"
        )
    )
    with pytest.raises(IntegrityError):
        await db.execute(
            text(
                "INSERT INTO certificates (id, user_id, serial_number, issuer, subject,"
                " pinfl_or_stir, valid_from, valid_to, status)"
                " VALUES (gen_random_uuid(), NULL, 'SER-1', 'ISS-1', 'CN=B', '12345678902',"
                " now(), now() + interval '1 year', 'active')"
            )
        )


@pytest.mark.asyncio
async def test_one_valid_signature_per_object_and_purpose(db, a_certificate):
    async def insert(status: str) -> None:
        await db.execute(
            text(
                "INSERT INTO signatures (id, object_type, object_id, purpose, signer_user_id,"
                " certificate_id, doc_hash, signature_value, signed_at, verification,"
                " verification_status)"
                " VALUES (gen_random_uuid(), 'permit',"
                " '00000000-0000-0000-0000-000000000001', 'permit_head', NULL,"
                f" '{a_certificate}', 'abc', 'PKCS7', now(), '{{}}'::jsonb, '{status}')"
            )
        )

    await insert("valid")
    await insert("invalid")  # an invalid attempt does not occupy the slot (ruling 8)
    with pytest.raises(IntegrityError):
        await insert("valid")


@pytest.mark.asyncio
async def test_signatures_permission_seeds(db):
    """prosecutor is oversight-read-only: view_any but not reverify (reverify writes
    a new signature row). central_admin holds both. sys_admin gets no row — it
    bypasses require_permission entirely (decision #41 ruling 2)."""
    rows = await db.execute(
        text(
            "SELECT r.code, rp.permission_code FROM role_permissions rp"
            " JOIN roles r ON r.id = rp.role_id"
            " WHERE rp.permission_code IN ('signatures.view_any', 'signatures.reverify')"
        )
    )
    assert {(row[0], row[1]) for row in rows} == {
        ("prosecutor", "signatures.view_any"),
        ("central_admin", "signatures.view_any"),
        ("central_admin", "signatures.reverify"),
    }
