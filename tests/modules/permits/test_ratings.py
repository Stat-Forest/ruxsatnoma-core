"""One rating per permit, 1-5, by its owner, after issuance (rulings #140-#142).

Task 3 laid the table and the read permission; Task 4 (below) adds the write —
`POST /permits/{id}/rating` — and folds the answer into the permit card. Task 5
(further below) adds the read side the Agency and each leshoz use: the zone-
scoped aggregates and the anonymous comment feed.
"""

import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.models import MediaFile
from app.modules.admin.models import Organization
from app.modules.gis.models import GisLayer
from app.modules.permits import repo, service
from app.modules.permits.models import Permit, PermitRating
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
from tests.modules.permits.conftest import Signer, make_permit_on_contour


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


async def test_the_card_hides_the_rating_from_a_required_signer_and_a_view_any_holder(
    active_permit: Permit,
    holder_client,
    head_client: Signer,
    hodim_client: httpx.AsyncClient,
) -> None:
    """Blocker 2, final review: `_readable_permit` admits three doors — the
    holder, any required signer of THIS permit, and any `permits.view_any`
    holder in zone — but ruling #141 forbids the rating reaching anyone but
    the holder alongside `applicant_id`/`series`/`number` (`PermitCardOut`
    extends `PermitOut`). `head_client` signed `permit_head` on
    `active_permit` (a required signer, `test_a_required_signer_may_read_but_
    not_rate`'s door) and `hodim_client` is `executor_staff`, the role
    migration 0019 grants `permits.view_any` to zone-free (`test_a_view_any_
    holder_may_read_but_not_rate`'s door) — both must read the card (they are
    not strangers) and both must get `rating: null`, on a permit that IS
    rated, while the holder gets the real thing.
    """
    rate = await holder_client.client.post(
        f"/api/v1/permits/{active_permit.id}/rating", json={"score": 2, "comment": "Sekin ishladi"}
    )
    assert rate.status_code == 201, rate.text

    signer_card = await head_client.client.get(f"/api/v1/permits/{active_permit.id}")
    assert signer_card.status_code == 200, signer_card.text
    assert signer_card.json()["rating"] is None

    view_any_card = await hodim_client.get(f"/api/v1/permits/{active_permit.id}")
    assert view_any_card.status_code == 200, view_any_card.text
    assert view_any_card.json()["rating"] is None

    holder_card = await holder_client.client.get(f"/api/v1/permits/{active_permit.id}")
    assert holder_card.status_code == 200, holder_card.text
    assert holder_card.json()["rating"]["score"] == 2
    assert holder_card.json()["rating"]["comment"] == "Sekin ishladi"


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


async def test_a_view_any_holder_may_read_but_not_rate(
    active_permit: Permit, hodim_client: httpx.AsyncClient
) -> None:
    """Fix round 1, finding A2: the named ownership case. `hodim_client` is
    built as `executor_staff`, migration 0019's own role — which is where the
    `permits.view_any` grant lives (`ROLE_GRANTS` in that migration), never
    only a personal grant — and the fixture is zone-free, which
    `_organization_in_actor_zone` treats as covering every organization,
    including this permit's own leshoz. So `_readable_permit` admits it
    through the `view_any` door (a different one than
    `test_a_required_signer_may_read_but_not_rate`'s required-signer door
    above), and `_is_holder` must still refuse it: reading a permit and
    having received its service are different questions."""
    response = await hodim_client.post(
        f"/api/v1/permits/{active_permit.id}/rating", json={"score": 5}
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "not_the_holder"


async def test_a_double_clicked_rating_answers_409_not_500(
    db: AsyncSession, active_permit: Permit, holder_client: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 1, finding A1: `repo.rating_for_permit`, read before the
    insert, is the common path to `already_rated` — this pins the OTHER one,
    the race a double-click can win. Stood in the same way
    `signatures/test_sign.py::test_a_racing_duplicate_signature_is_also_audited`
    stands in for its own race: a rating already exists, committed, as the
    loser of a real race would find one; the pre-check is monkeypatched stale
    so the call believes none exists, and the real INSERT is what has to
    raise. Against the unguarded code (no `begin_nested()`, no `except
    IntegrityError`) this is an uncaught `IntegrityError` on
    `ix_permit_ratings_permit_id` — a 500 — and the fix maps it to the SAME
    409 `already_rated` the ordinary pre-check gives."""
    db.add(PermitRating(permit_id=active_permit.id, score=3))
    await db.commit()

    async def _stale_no_rating(db: AsyncSession, permit_id):
        return None  # stands in for the race: the other call already committed

    monkeypatch.setattr(repo, "rating_for_permit", _stale_no_rating)

    with pytest.raises(DomainError) as exc:
        await service.rate_permit(
            db, active_permit.id, score=4, comment=None, actor=holder_client.user
        )
    assert exc.value.code == "ERR-PERM-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "already_rated"
    # No second row: the loser's own insert never lands.
    ratings = (
        await db.execute(
            text("SELECT count(*) FROM permit_ratings WHERE permit_id = :p"),
            {"p": str(active_permit.id)},
        )
    ).scalar_one()
    assert ratings == 1


# --- Task 5: the Agency's aggregates, without the author ----------------------


@pytest.fixture
def ratings_client(head_client: Signer) -> httpx.AsyncClient:
    """`executor_head` of `leshoz` — migration 0039 grants `ratings.view` to
    the ROLE (`test_the_prosecutor_holds_the_new_read_permission` above proves
    the same grant for `prosecutor`), so `head_client`'s own client is exactly
    a `ratings.view` holder zoned to `leshoz`."""
    return head_client.client


@pytest.fixture
def other_org_ratings_client(other_org_head_client: Signer) -> httpx.AsyncClient:
    """`executor_head` of a DIFFERENT leshoz: same role, same grant, a
    different zone (lesson: zone scoping is not a permission check)."""
    return other_org_head_client.client


@pytest.fixture
def staff_client(hodim_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """`executor_staff` — holds `permits.issue` only. Migration 0039 grants
    `ratings.view` to `central_admin`, `leadership`, `executor_head` and
    `prosecutor`; this role is none of the four, so it is the route's own
    closed-without-the-permission case."""
    return hodim_client


@pytest.fixture
async def seeded_ratings(
    db: AsyncSession,
    leshoz: Organization,
    contours_layer: GisLayer,
    approval_doc: MediaFile,
    grazing_activity_id: uuid.UUID,
    haymaking_activity_id: uuid.UUID,
) -> list[PermitRating]:
    """Three ratings on THIS test's own `leshoz` — a fresh organization every
    run (`leshoz`'s own fixture builds one, never reuses another test's), so
    the zone-scoped assertions below cannot be satisfied by a row a different
    test left on this shared, persistent database (lesson: "the test DB is
    shared, persistent and never empty"). Scores 3, 4, 5 average to exactly
    4.00; two activity types so `by_activity_type` has more than one group to
    prove it groups at all.

    Inserted directly through the ORM, never through `POST /permits/{id}/
    rating`: that route enforces "the holder, once" (ruling #140), which is
    not what this fixture exists to prove. Only flushed, not committed —
    `ratings_client`'s own `_commit_pending_before_requests` hook (built into
    `head_client`) commits everything pending on `db` right before the test's
    first request fires, whatever order pytest builds the fixtures in.
    """
    contour = await make_contour(db, contours_layer, leshoz)
    version = await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    ratings = []
    for activity_type_id, score in (
        (grazing_activity_id, 3),
        (haymaking_activity_id, 4),
        (grazing_activity_id, 5),
    ):
        permit = await make_permit_on_contour(
            db,
            contour=contour,
            version_id=version.id,
            org=leshoz,
            activity_type_id=activity_type_id,
            status="active",
        )
        rating = PermitRating(permit_id=permit.id, score=score, comment=f"score {score}")
        db.add(rating)
        ratings.append(rating)
    await db.flush()
    return ratings


async def test_summary_averages_by_organization_and_activity(
    ratings_client, seeded_ratings
) -> None:
    body = (
        await ratings_client.get(
            "/api/v1/admin/ratings/summary",
            params={"period_from": "2026-01-01", "period_to": "2026-12-31"},
        )
    ).json()
    assert body["count"] == 3
    assert body["avg_score"] == "4.00"
    assert {row["organization_id"] for row in body["by_organization"]}
    assert {row["activity_type_id"] for row in body["by_activity_type"]}


async def test_comments_never_name_the_author(ratings_client, seeded_ratings) -> None:
    """Ruling #141. Asserted on the serialized body, not on the schema: a field
    added to a nested model later would pass a field-name check and still leak."""
    raw = (
        await ratings_client.get(
            "/api/v1/admin/ratings",
            params={"period_from": "2026-01-01", "period_to": "2026-12-31"},
        )
    ).text
    for forbidden in ("applicant", "user_id", "full_name", "pinfl", "permit_number", "permit_id"):
        assert forbidden not in raw, f"ruling #141: {forbidden} reached the comments feed"


async def test_a_leshoz_sees_only_its_own(other_org_ratings_client, seeded_ratings) -> None:
    body = (
        await other_org_ratings_client.get(
            "/api/v1/admin/ratings/summary",
            params={"period_from": "2026-01-01", "period_to": "2026-12-31"},
        )
    ).json()
    assert body["count"] == 0


async def test_the_feed_narrows_by_activity_type_the_same_as_the_summary(
    ratings_client,
    seeded_ratings: list[PermitRating],
    grazing_activity_id: uuid.UUID,
    haymaking_activity_id: uuid.UUID,
) -> None:
    """Final review, finding 3: `GET /admin/ratings/summary` accepts
    `activity_type_id` and narrows; `GET /admin/ratings` must narrow the same
    way or the comments below the summary describe a different population.
    `seeded_ratings` puts two grazing ratings (scores 3, 5) and one haymaking
    rating (score 4) on the same leshoz."""
    unfiltered = (
        await ratings_client.get(
            "/api/v1/admin/ratings",
            params={"period_from": "2026-01-01", "period_to": "2026-12-31"},
        )
    ).json()
    assert unfiltered["total"] == 3

    grazing = (
        await ratings_client.get(
            "/api/v1/admin/ratings",
            params={
                "period_from": "2026-01-01",
                "period_to": "2026-12-31",
                "activity_type_id": str(grazing_activity_id),
            },
        )
    ).json()
    assert grazing["total"] == 2
    assert {row["score"] for row in grazing["items"]} == {3, 5}

    haymaking = (
        await ratings_client.get(
            "/api/v1/admin/ratings",
            params={
                "period_from": "2026-01-01",
                "period_to": "2026-12-31",
                "activity_type_id": str(haymaking_activity_id),
            },
        )
    ).json()
    assert haymaking["total"] == 1
    assert haymaking["items"][0]["score"] == 4


async def test_the_feed_narrows_by_organization_the_same_as_the_summary(
    ratings_client, seeded_ratings: list[PermitRating], leshoz: Organization
) -> None:
    """Same finding, the other filter: `organization_id` matching the actor's
    own leshoz keeps every row, a different organization id (still inside the
    zone-free `head_client`'s own single-org zone) empties the feed — proof
    the parameter reaches the query rather than being silently accepted and
    ignored."""
    matching = (
        await ratings_client.get(
            "/api/v1/admin/ratings",
            params={
                "period_from": "2026-01-01",
                "period_to": "2026-12-31",
                "organization_id": str(leshoz.id),
            },
        )
    ).json()
    assert matching["total"] == 3

    other = (
        await ratings_client.get(
            "/api/v1/admin/ratings",
            params={
                "period_from": "2026-01-01",
                "period_to": "2026-12-31",
                "organization_id": str(uuid.uuid4()),
            },
        )
    ).json()
    assert other["total"] == 0


async def test_the_route_is_closed_without_the_permission(staff_client) -> None:
    assert (
        await staff_client.get(
            "/api/v1/admin/ratings/summary",
            params={"period_from": "2026-01-01", "period_to": "2026-12-31"},
        )
    ).status_code == 403
