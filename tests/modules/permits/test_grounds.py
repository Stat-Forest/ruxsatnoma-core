import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.admin import repo as admin_repo
from app.modules.permits import grounds


async def _item(db: AsyncSession, code: str):
    classifier = await admin_repo.get_classifier_by_code(db, grounds.CLASSIFIER_CODE)
    assert classifier is not None, "migration 0023 must seed the classifier"
    items = await admin_repo.list_classifier_items(db, classifier.id)
    found = next((row for row in items if row.code == code), None)
    assert found is not None, f"migration 0023 must seed {code}"
    return found


async def test_all_seven_grounds_are_seeded_and_active(db: AsyncSession) -> None:
    """Ruling 5's table, in the database rather than in prose."""
    classifier = await admin_repo.get_classifier_by_code(db, grounds.CLASSIFIER_CODE)
    assert classifier is not None
    items = await admin_repo.list_classifier_items(db, classifier.id)
    assert {row.code for row in items} == {f"PS-{n:02d}" for n in range(1, 8)}
    assert all(row.status == "active" for row in items)


async def test_a_suspension_ground_cannot_justify_a_resume(db: AsyncSession) -> None:
    """PS-04 is the fire-danger restriction: it suspends, it never resumes."""
    item = await _item(db, "PS-04")
    with pytest.raises(DomainError) as caught:
        await grounds.assert_applicable(
            db, reason_item_id=item.id, act=grounds.RESUME, legal_basis="фойдаланилмади"
        )
    assert caught.value.code == "ERR-VAL-001"
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "reason_not_applicable"


async def test_ps07_demands_its_own_explanation(db: AsyncSession) -> None:
    """«Бошқа (изоҳ мажбурий)» — the same rule RJ-15 carries for applications."""
    item = await _item(db, "PS-07")
    with pytest.raises(DomainError) as caught:
        await grounds.assert_applicable(
            db, reason_item_id=item.id, act=grounds.SUSPEND, legal_basis="   "
        )
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "legal_basis_required"

    kept = await grounds.assert_applicable(
        db, reason_item_id=item.id, act=grounds.SUSPEND, legal_basis="Суд қарори №12"
    )
    assert kept.code == "PS-07"


async def test_a_reason_from_another_classifier_is_refused(db: AsyncSession) -> None:
    """`permit_status_history.reason_item_id` FKs the whole of `classifier_items`,
    so the FK cannot tell an RJ-* apart from a PS-* — this function can."""
    rejection = await admin_repo.get_classifier_by_code(db, "rejection_reasons")
    assert rejection is not None
    rj = (await admin_repo.list_classifier_items(db, rejection.id))[0]
    with pytest.raises(DomainError) as caught:
        await grounds.assert_applicable(
            db, reason_item_id=rj.id, act=grounds.REVOKE, legal_basis="x"
        )
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "reason_wrong_classifier"


async def test_an_unknown_id_is_a_validation_error_not_a_404(db: AsyncSession) -> None:
    with pytest.raises(DomainError) as caught:
        await grounds.assert_applicable(
            db, reason_item_id=uuid.uuid4(), act=grounds.SUSPEND, legal_basis="x"
        )
    assert caught.value.code == "ERR-VAL-001"
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "reason_not_found"
