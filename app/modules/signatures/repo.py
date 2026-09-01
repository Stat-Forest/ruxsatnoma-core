"""Queries for certificates and signatures. Nothing here decides anything — an
"existing valid signature" or "a certificate already bound to someone" is a
fact the service reads and interprets, not a policy this module enforces."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.integrations.adapters.eimzo import EimzoCertificateInfo
from app.modules.signatures.models import Certificate, Signature


async def get_certificate(db: AsyncSession, certificate_id: uuid.UUID) -> Certificate | None:
    return await db.get(Certificate, certificate_id)


async def get_certificate_by_identity(
    db: AsyncSession, serial_number: str, issuer: str
) -> Certificate | None:
    """A certificate's identity is the `(serial_number, issuer)` pair
    (`uq_certificate_identity`) — never the id alone, since the caller never
    has an id until this lookup already found one."""
    rows = await db.execute(
        select(Certificate).where(
            Certificate.serial_number == serial_number, Certificate.issuer == issuer
        )
    )
    return rows.scalar_one_or_none()


async def insert_certificate(
    db: AsyncSession, *, info: EimzoCertificateInfo, user_id: uuid.UUID | None
) -> Certificate:
    """A brand-new certificate is always `active`, written with whatever
    `user_id` the caller passes — the presenting user's id once ownership is
    proven, `None` when it is not yet (fix round 1, ruling 1: an unproven
    first bind is still recorded as evidence, just left unbound). The
    service decides both WHETHER to call this and what `user_id` to pass;
    this function only ever writes the row it's given."""
    cert = Certificate(
        user_id=user_id,
        serial_number=info.serial_number,
        issuer=info.issuer,
        subject=info.subject,
        pinfl_or_stir=info.pinfl_or_stir,
        valid_from=info.valid_from,
        valid_to=info.valid_to,
        status="active",
    )
    db.add(cert)
    await db.flush()
    return cert


async def get_valid_signature(
    db: AsyncSession, object_type: str, object_id: uuid.UUID, purpose: str
) -> Signature | None:
    """Mirrors `uq_signatures_valid_purpose`'s own `WHERE` clause exactly — an
    `invalid` attempt never occupies the slot (ruling 8), so this only ever
    looks at `valid` rows, the same rows the index itself covers."""
    rows = await db.execute(
        select(Signature).where(
            Signature.object_type == object_type,
            Signature.object_id == object_id,
            Signature.purpose == purpose,
            Signature.verification_status == "valid",
        )
    )
    return rows.scalar_one_or_none()


async def insert_signature(
    db: AsyncSession,
    *,
    object_type: str,
    object_id: uuid.UUID,
    purpose: str,
    signer_user_id: uuid.UUID | None,
    certificate_id: uuid.UUID,
    doc_hash: str,
    signature_value: str,
    signed_at: datetime,
    verification: dict[str, Any],
    verification_status: str,
) -> Signature:
    """Evidence, not state (`models.py`): written for a valid AND an invalid
    verdict alike — the service decides which, this function just stores it."""
    signature = Signature(
        object_type=object_type,
        object_id=object_id,
        purpose=purpose,
        signer_user_id=signer_user_id,
        certificate_id=certificate_id,
        doc_hash=doc_hash,
        signature_value=signature_value,
        signed_at=signed_at,
        verification=verification,
        verification_status=verification_status,
    )
    db.add(signature)
    await db.flush()
    return signature


async def list_for_object(
    db: AsyncSession, *, object_type: str, object_id: uuid.UUID
) -> list[Signature]:
    """Every signature attempt against one object, any purpose, any outcome —
    oversight (4.2) and a permit's own signature list both read through this."""
    rows = await db.execute(
        select(Signature)
        .where(Signature.object_type == object_type, Signature.object_id == object_id)
        .order_by(Signature.signed_at)
    )
    return list(rows.scalars())


async def get_signature(db: AsyncSession, signature_id: uuid.UUID) -> Signature | None:
    return await db.get(Signature, signature_id)


async def signed_by(
    db: AsyncSession, *, object_type: str, object_id: uuid.UUID, signer_user_id: uuid.UUID
) -> bool:
    """Whether `signer_user_id` has at least one signature row — valid or
    invalid, an attempt is still evidence (ruling 8) — against this object.
    Task 7's own definition of "the object's owner" for `GET /signatures`: the
    only ownership signal available to a module that owns no
    `permits`/`applications` table of its own to ask (module docstring,
    Level 2 — this module never queries another module's tables)."""
    rows = await db.execute(
        select(Signature.id)
        .where(
            Signature.object_type == object_type,
            Signature.object_id == object_id,
            Signature.signer_user_id == signer_user_id,
        )
        .limit(1)
    )
    return rows.first() is not None


async def list_for_object_page(
    db: AsyncSession, *, object_type: str, object_id: uuid.UUID, offset: int, limit: int
) -> tuple[list[Signature], int]:
    """The paged twin of `list_for_object` above — that one stays UNPAGED
    forever, since `missing_purposes`/`is_complete` read EVERY row through it
    and silently truncating to page 1 would corrupt a completeness answer
    (lesson: paging a list breaks assumed membership). This is Task 7's own,
    for `GET /signatures` only, ordered `(signed_at, id)` — `signed_at` alone
    ties whenever two signatures land in the same instant, and an unordered
    tie leaves `items[0]` to Postgres' own row order (pre-flight ruling P6)."""
    conditions = (Signature.object_type == object_type, Signature.object_id == object_id)
    total = (
        await db.execute(select(func.count()).select_from(Signature).where(*conditions))
    ).scalar_one()
    rows = await db.execute(
        select(Signature)
        .where(*conditions)
        .order_by(Signature.signed_at, Signature.id)
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars()), total


async def list_certificates(
    db: AsyncSession, *, user_id: uuid.UUID, offset: int, limit: int
) -> tuple[list[Certificate], int]:
    """A user's own BOUND certificates (`unbound_at IS NULL`) — Task 7's own
    `GET /certificates`. Unbinding (below) never deletes a row, so a
    certificate a signature still references keeps existing, just off this
    list."""
    conditions = (Certificate.user_id == user_id, Certificate.unbound_at.is_(None))
    total = (
        await db.execute(select(func.count()).select_from(Certificate).where(*conditions))
    ).scalar_one()
    rows = await db.execute(
        select(Certificate)
        .where(*conditions)
        .order_by(Certificate.bound_at, Certificate.id)
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars()), total
