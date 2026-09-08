"""The legal-documents register (`0043`): the lifecycle it copies from
announcements, the ordering that is its own, and the one rule neither of the
other content tables has — a row may not be published with nothing to open."""

from datetime import date

import pytest

from app.core.errors import DomainError
from app.core.schemas import PageParams
from app.modules.admin import legal_documents_service as service
from app.modules.admin.legal_documents_service import (
    LegalDocumentCreateIn,
    LegalDocumentPatchIn,
)
from app.modules.admin.permissions import LEGAL_DOCUMENTS_MANAGE
from tests.modules.admin.test_organizations_admin import signed_in_with

CODE = {"uz_latn": "O'rmon kodeksi", "ru": "Лесной кодекс"}
LEX = "https://lex.uz/docs/3799819"


async def make_draft(db, actor, **overrides):
    data = {
        "title": CODE,
        "doc_number": "ZRU-475",
        "adopted_on": date(2018, 4, 16),
    }
    data.update(overrides)
    return await service.create(db, data=LegalDocumentCreateIn(**data), actor=actor)


async def test_publish_refuses_a_row_with_neither_a_file_nor_a_link(db):
    """A published row whose button opens nothing is exactly the broken page
    this register replaces — the button was there and led nowhere."""
    actor, _, _ = await signed_in_with(db, LEGAL_DOCUMENTS_MANAGE)
    doc = await make_draft(db, actor)

    with pytest.raises(DomainError) as raised:
        await service.publish(db, doc_id=doc.id, actor=actor)

    assert raised.value.code == "ERR-VAL-001"
    assert raised.value.details == {"reason": "nothing_to_open"}
    assert (await service.get_admin(db, doc_id=doc.id)).status == "draft"


async def test_publish_accepts_a_row_that_only_has_a_link(db):
    """Half the register is on lex.uz already, and nobody will scan the Forest
    Code to publish a link to it."""
    actor, _, _ = await signed_in_with(db, LEGAL_DOCUMENTS_MANAGE)
    doc = await make_draft(db, actor, source_url=LEX)

    published = await service.publish(db, doc_id=doc.id, actor=actor)

    assert published.status == "published"
    assert published.source_url == LEX


async def test_publish_is_refused_twice_and_on_an_archived_row(db):
    actor, _, _ = await signed_in_with(db, LEGAL_DOCUMENTS_MANAGE)
    doc = await make_draft(db, actor, source_url=LEX)
    await service.publish(db, doc_id=doc.id, actor=actor)

    with pytest.raises(DomainError) as already:
        await service.publish(db, doc_id=doc.id, actor=actor)
    assert already.value.details == {"reason": "already_published"}

    await service.archive(db, doc_id=doc.id, actor=actor)
    with pytest.raises(DomainError) as archived:
        await service.publish(db, doc_id=doc.id, actor=actor)
    assert archived.value.details == {"reason": "archived"}


async def test_patch_refuses_an_archived_row(db):
    actor, _, _ = await signed_in_with(db, LEGAL_DOCUMENTS_MANAGE)
    doc = await make_draft(db, actor, source_url=LEX)
    await service.archive(db, doc_id=doc.id, actor=actor)

    with pytest.raises(DomainError) as raised:
        await service.patch(
            db, doc_id=doc.id, data=LegalDocumentPatchIn(doc_number="ZRU-476"), actor=actor
        )
    assert raised.value.details == {"reason": "archived"}


async def test_the_public_list_orders_by_sort_order_then_newest_adopted(db):
    """The Forest Code heads the page forever, and by date of adoption it is the
    oldest row in the register — which is the whole reason `sort_order` leads."""
    actor, _, _ = await signed_in_with(db, LEGAL_DOCUMENTS_MANAGE)
    # Numbers unique to this test, and negative `sort_order`s so these three head
    # the register whatever else the API suites have committed into the shared
    # test database: `page_size` is capped at 100, so a row that sorts after
    # somebody else's hundred would simply not be on the page being asserted.
    for number, adopted, order in (
        ("ORD-PF-108", date(2026, 1, 1), -10),
        ("ORD-ZRU-475", date(2018, 4, 16), -20),
        ("ORD-VMQ-342", date(2021, 5, 12), -10),
    ):
        doc = await make_draft(
            db, actor, doc_number=number, adopted_on=adopted, sort_order=order, source_url=LEX
        )
        await service.publish(db, doc_id=doc.id, actor=actor)

    page = await service.list_public(db, params=PageParams(page=1, page_size=100))
    ours = [item.doc_number for item in page.items if item.doc_number.startswith("ORD-")]

    assert ours == ["ORD-ZRU-475", "ORD-PF-108", "ORD-VMQ-342"]


async def test_only_published_rows_reach_the_public_surface(db):
    actor, _, _ = await signed_in_with(db, LEGAL_DOCUMENTS_MANAGE)
    draft = await make_draft(db, actor, doc_number="DRAFT-1", source_url=LEX)
    archived = await make_draft(db, actor, doc_number="ARCH-1", source_url=LEX)
    await service.publish(db, doc_id=archived.id, actor=actor)
    await service.archive(db, doc_id=archived.id, actor=actor)
    live = await make_draft(db, actor, doc_number="LIVE-1", source_url=LEX)
    await service.publish(db, doc_id=live.id, actor=actor)

    page = await service.list_public(db, params=PageParams(page=1, page_size=100))
    listed = {item.id for item in page.items}

    assert live.id in listed
    assert draft.id not in listed
    assert archived.id not in listed
    for hidden in (draft, archived):
        with pytest.raises(DomainError) as raised:
            await service.get_public(db, doc_id=hidden.id)
        assert raised.value.code == "ERR-SYS-003"


async def test_the_public_shape_carries_no_admin_internals(db):
    """`status`, `sort_order` and `created_by` are editorial bookkeeping: the
    citizen gets what the page prints and nothing else."""
    actor, _, _ = await signed_in_with(db, LEGAL_DOCUMENTS_MANAGE)
    doc = await make_draft(db, actor, source_url=LEX, summary={"uz_latn": "Qisqacha"})
    await service.publish(db, doc_id=doc.id, actor=actor)

    public = await service.get_public(db, doc_id=doc.id)
    fields = public.model_dump()

    assert set(fields) == {
        "id",
        "title",
        "summary",
        "doc_number",
        "adopted_on",
        "source_url",
        "file",
    }
    assert fields["title"] == CODE
    assert fields["file"] is None


async def test_admin_list_filters_by_status(db):
    actor, _, _ = await signed_in_with(db, LEGAL_DOCUMENTS_MANAGE)
    draft = await make_draft(db, actor, doc_number="D-1")
    live = await make_draft(db, actor, doc_number="P-1", source_url=LEX)
    await service.publish(db, doc_id=live.id, actor=actor)

    drafts = await service.list_admin(db, params=PageParams(page=1, page_size=100), status="draft")
    ids = {item.id for item in drafts.items}

    assert draft.id in ids
    assert live.id not in ids
