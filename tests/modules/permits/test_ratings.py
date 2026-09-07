"""One rating per permit, 1-5, by its owner, after issuance (rulings #140-#142).

Task 3 laid the table and the read permission; Task 4 (below) adds the write —
`POST /permits/{id}/rating` — and folds the answer into the permit card.
"""

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


# --- Task 4: the citizen rates their permit through the API ------------------


async def test_the_owner_rates_an_issued_permit_once(active_permit: Permit, holder_client) -> None:
    first = await holder_client.client.post(
        f"/api/v1/permits/{active_permit.id}/rating", json={"score": 4, "comment": "Tez va qulay"}
    )
    assert first.status_code == 201, first.text
    assert first.json()["score"] == 4
    assert first.json()["comment"] == "Tez va qulay"

    second = await holder_client.client.post(
        f"/api/v1/permits/{active_permit.id}/rating", json={"score": 1}
    )
    assert second.status_code == 409, "ruling #140: one rating per permit"
    assert second.json()["error"]["details"]["reason"] == "already_rated"


async def test_the_card_carries_the_rating_so_the_cabinet_needs_one_request(
    active_permit: Permit, holder_client
) -> None:
    before = await holder_client.client.get(f"/api/v1/permits/{active_permit.id}")
    assert before.json()["rating"] is None

    await holder_client.client.post(f"/api/v1/permits/{active_permit.id}/rating", json={"score": 5})

    after = await holder_client.client.get(f"/api/v1/permits/{active_permit.id}")
    assert after.json()["rating"]["score"] == 5


async def test_a_permit_still_awaiting_signatures_cannot_be_rated(
    issued_permit: Permit, holder_client
) -> None:
    response = await holder_client.client.post(
        f"/api/v1/permits/{issued_permit.id}/rating", json={"score": 5}
    )
    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "not_issued"


async def test_a_stranger_gets_the_same_404_the_card_gives(
    active_permit: Permit, other_applicant_client
) -> None:
    """`_readable_permit`'s own rule (router docstring): two different answers make
    the route a permit-existence oracle."""
    response = await other_applicant_client.client.post(
        f"/api/v1/permits/{active_permit.id}/rating", json={"score": 5}
    )
    assert response.status_code == 404


async def test_a_required_signer_may_read_but_not_rate(active_permit: Permit, head_client) -> None:
    """Ruling #140/decisions: readability and ownership are different questions.
    `head_client` is a required signer of THIS permit (it signed `permit_head`
    in `active_permit`), so `_readable_permit` admits it — and must still be
    refused here, the same as a `permits.view_any` holder would be."""
    response = await head_client.client.post(
        f"/api/v1/permits/{active_permit.id}/rating", json={"score": 5}
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "not_the_holder"


async def test_score_zero_and_six_are_refused_before_the_database_sees_them(
    active_permit: Permit, holder_client
) -> None:
    for bad in (0, 6):
        response = await holder_client.client.post(
            f"/api/v1/permits/{active_permit.id}/rating", json={"score": bad}
        )
        assert response.status_code == 422
