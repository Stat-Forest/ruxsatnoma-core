"""`legal_documents` (`0043`) — the register behind the public site's
"Normativ-huquqiy hujjatlar" page. The table's own guarantees: the three-value
status CHECK and the defaults a row keeps when the editor fills in nothing but
the four fields the page shows."""

import uuid
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.admin.models import LegalDocument
from tests.modules.auth.test_sessions import make_user


async def test_status_is_constrained_to_the_three_lifecycle_values(db):
    actor = await make_user(db, role_code="sys_admin")
    db.add(
        LegalDocument(
            title={"uz_latn": "O'rmon kodeksi"},
            doc_number="ZRU-475",
            adopted_on=date(2018, 4, 16),
            status="deleted",
            created_by=actor.id,
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_a_bare_row_keeps_its_defaults(db):
    actor = await make_user(db, role_code="sys_admin")
    doc = LegalDocument(
        title={"uz_latn": "O'rmon kodeksi"},
        doc_number="ZRU-475",
        adopted_on=date(2018, 4, 16),
        created_by=actor.id,
    )
    db.add(doc)
    await db.flush()

    assert isinstance(doc.id, uuid.UUID)
    assert doc.status == "draft"
    assert doc.sort_order == 0
    # The two ways a row can point at something to open, both optional at draft
    # time: `publish` is where their absence becomes an error, not the table.
    assert doc.file_id is None
    assert doc.source_url is None
    assert doc.summary is None
