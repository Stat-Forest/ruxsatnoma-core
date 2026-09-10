"""Every REDUCED view of a `signatures` row must accept every row the table
can hold.

Stage 10's review found both views — `permits.schemas.PermitSignatureRow` and
`applications.schemas.TimelineSignatureRow` — typing `certificate_id` as a
bare `uuid.UUID` after migration `0052` had made the column nullable for a
`kind='simple'` row (ruling #183): the signing routes were green, and the
permit card and the application timeline answered 500 on the very next read.
`from_attributes` views are copies of the model's columns by hand, so this
is the check that a widened column reaches every copy — the mechanical form
of the `response_model` lesson.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from app.modules.applications.schemas import TimelineSignatureRow
from app.modules.permits.schemas import PermitSignatureRow
from app.modules.signatures.models import SIGNATURE_KINDS, Signature

REDUCED_VIEWS: tuple[type[BaseModel], ...] = (PermitSignatureRow, TimelineSignatureRow)


def _row(kind: str) -> Signature:
    return Signature(
        id=uuid.uuid4(),
        object_type="permit",
        object_id=uuid.uuid4(),
        purpose="permit_recipient",
        signer_user_id=uuid.uuid4(),
        kind=kind,
        certificate_id=None if kind == "simple" else uuid.uuid4(),
        doc_hash="deadbeef",
        signature_value="" if kind == "simple" else "MIIB...",
        signed_at=datetime.now(UTC),
        verification={"kind": kind},
        verification_status="valid",
    )


@pytest.mark.parametrize("view", REDUCED_VIEWS, ids=lambda v: v.__name__)
@pytest.mark.parametrize("kind", SIGNATURE_KINDS)
def test_every_reduced_view_accepts_every_kind_of_signature_row(view, kind):
    out = view.model_validate(_row(kind))
    assert out.kind == kind  # type: ignore[attr-defined]
    assert (out.certificate_id is None) == (kind == "simple")  # type: ignore[attr-defined]


def test_the_views_carry_the_kind_and_a_nullable_certificate():
    """The shape itself, so a future copy of the row cannot drop either."""
    for view in REDUCED_VIEWS:
        fields = view.model_fields
        assert "kind" in fields, view.__name__
        assert fields["certificate_id"].annotation == (uuid.UUID | None), view.__name__
