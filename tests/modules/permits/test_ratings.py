"""One rating per permit, 1-5, by its owner, after issuance (rulings #140-#142)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.modules.permits.models import Permit, PermitRating


async def test_score_outside_one_to_five_is_refused_by_the_database(
    db, active_permit: Permit
) -> None:
    db.add(PermitRating(permit_id=active_permit.id, score=6))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_a_permit_accepts_only_one_rating(db, active_permit: Permit) -> None:
    db.add(PermitRating(permit_id=active_permit.id, score=5))
    await db.flush()
    db.add(PermitRating(permit_id=active_permit.id, score=1))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_the_prosecutor_holds_the_new_read_permission(db) -> None:
    """Decision #95 and `tests/test_permissions_registry.py`'s suffix rule: a `*.view`
    code that skips this grant turns that whole test red, not this one."""
    held = await db.execute(
        text(
            "SELECT 1 FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
            "WHERE r.code = 'prosecutor' AND rp.permission_code = 'ratings.view'"
        )
    )
    assert held.scalar() == 1
