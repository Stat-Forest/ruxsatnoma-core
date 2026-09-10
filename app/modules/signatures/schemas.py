"""API shapes for certificates and signatures (Task 7). Output schemas mirror
the ORM rows verbatim (`ConfigDict(from_attributes=True)`, the same idiom
`norms/schemas.py` uses) -- this module stores evidence, not a projection, so
there is no reason for the wire shape to diverge from the stored one."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


class CertificateBindIn(BaseModel):
    """`POST /certificates`: a self-contained signed challenge. E-IMZO's
    ATTACHED form carries what was signed inside the envelope itself, so
    there is no separate document to hand alongside it -- unlike `sign()`,
    which signs bytes the caller supplies (ruling 6), this route has no
    document of its own (ruling 4's explicit-bind path)."""

    pkcs7: Annotated[str, Field(min_length=1)]


class CertificateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    serial_number: str
    issuer: str
    subject: str
    pinfl_or_stir: str
    valid_from: datetime
    valid_to: datetime
    status: str
    bound_at: datetime
    revoked_at: datetime | None


class SignatureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    object_type: str
    object_id: uuid.UUID
    purpose: str
    kind: str
    signer_user_id: uuid.UUID | None
    # NULL exactly for `kind="simple"` (ruling #183, migration 0052) — a
    # simple signature presents no certificate at all.
    certificate_id: uuid.UUID | None
    doc_hash: str
    signature_value: str
    signed_at: datetime
    verification: dict[str, Any]
    verification_status: str
