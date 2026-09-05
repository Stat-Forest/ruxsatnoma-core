"""Task 6: the forest ticket (ЧТ), ўрмон чиптаси, ВМҚ 506 — the writer and
register 3.11a's own migration (0019/0023) created `forest_tickets` without.

Four decisions this file assumes rather than re-derives:

  * **`permits.manage` gates both routes** (ruling 2, the gate Task 6's own
    brief left unnamed — filled in during the preflight scan the same way
    ruling 14 below was revised). Both routes are DELIBERATELY narrower than
    `/duplicates`'s pair: a ticket is issued and revoked by the leshoz that
    MANAGES the permit. `hodim_client` (`permits.issue` only, `executor_staff`)
    would 403 here — it is `head_client` (`executor_head`, `permits.manage`
    through migration 0019) that every write and read test below drives
    (lesson: a fixture's grants must be checked, not assumed from its name).
  * Ruling 12 — a ticket only ever rides an ACTIVE permit.
  * Ruling 13 — `uq_forest_tickets_active`, surfaced as a named 409
    (`ERR-PERM-003`), caught by constraint NAME so an unrelated FK violation
    on the same INSERT is never mislabeled a ticket conflict.
  * Ruling 14 (revised) — a REVOKED permit takes its live ticket down with it
    in the same transaction; a SUSPENDED one does not, and
    `jobs.expire_forest_tickets` is its own STANDALONE sweep, never a third
    statement folded into `expire_permits`'s batch loop.
"""

from datetime import timedelta
from typing import get_args

from app.modules.permits import jobs, repo
from app.modules.permits.models import FOREST_TICKET_STATUSES
from app.modules.permits.schemas import ForestTicketStatus
from tests.modules.permits.conftest import sign_decision


def _body(active_permit, **overrides) -> dict:
    body = {
        "valid_from": str(active_permit.period_from),
        "valid_to": str(active_permit.period_to),
        "restrictions": {},
    }
    body.update(overrides)
    return body


async def test_a_ticket_gets_a_cyrillic_cht_number(active_permit, head_client) -> None:
    created = await head_client.client.post(
        f"/api/v1/permits/{active_permit.id}/forest-tickets",
        json=_body(active_permit, restrictions={"notes": "ёнғин хавфи юқори"}),
    )
    assert created.status_code == 201, created.text
    number = created.json()["number"]
    assert number.startswith("ЧТ-") and len(number.split("-")[-1]) == 6


async def test_a_ticket_may_not_outlive_its_permit(active_permit, head_client) -> None:
    """Ruling 12: a licence for a day the permit does not cover."""
    refused = await head_client.client.post(
        f"/api/v1/permits/{active_permit.id}/forest-tickets",
        json=_body(active_permit, valid_to=str(active_permit.period_to + timedelta(days=1))),
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["reason"] == "period_outside_permit"


async def test_only_one_live_ticket_per_permit(active_permit, head_client) -> None:
    """Ruling 13 — the partial unique index, surfaced as a named 409."""
    body = _body(active_permit)
    first = await head_client.client.post(
        f"/api/v1/permits/{active_permit.id}/forest-tickets", json=body
    )
    assert first.status_code == 201
    second = await head_client.client.post(
        f"/api/v1/permits/{active_permit.id}/forest-tickets", json=body
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ERR-PERM-003"
    assert second.json()["error"]["details"]["reason"] == "active_ticket_exists"


async def test_revoking_the_permit_revokes_its_ticket(
    db, active_permit, head_client, revoke_reason_id, order_file_id
) -> None:
    """Ruling 14: an inspector must not find a live licence under a cancelled permit."""
    await head_client.client.post(
        f"/api/v1/permits/{active_permit.id}/forest-tickets", json=_body(active_permit)
    )
    await sign_decision(
        head_client,
        active_permit.id,
        "revoke",
        reason_item_id=revoke_reason_id,
        doc_file_id=order_file_id,
    )
    tickets = await repo.forest_tickets(db, active_permit.id)
    assert [t.status for t in tickets] == ["revoked"]


async def test_a_suspension_leaves_the_ticket_alone(
    db, active_permit, head_client, suspend_reason_id, order_file_id
) -> None:
    """A suspension is temporary; a resume must not have to reissue paperwork."""
    await head_client.client.post(
        f"/api/v1/permits/{active_permit.id}/forest-tickets", json=_body(active_permit)
    )
    await sign_decision(
        head_client,
        active_permit.id,
        "suspend",
        reason_item_id=suspend_reason_id,
        doc_file_id=order_file_id,
    )
    tickets = await repo.forest_tickets(db, active_permit.id)
    assert [t.status for t in tickets] == ["active"]


async def test_an_expired_ticket_is_swept(db, active_permit, head_client, monkeypatch) -> None:
    """`business_today()` — Asia/Tashkent — and never `date.today()`."""
    await head_client.client.post(
        f"/api/v1/permits/{active_permit.id}/forest-tickets", json=_body(active_permit)
    )
    monkeypatch.setattr(jobs, "business_today", lambda: active_permit.period_to + timedelta(days=1))
    await jobs.expire_forest_tickets(db)
    assert [t.status for t in await repo.forest_tickets(db, active_permit.id)] == ["expired"]


async def test_a_ticket_needs_an_active_permit(issued_permit, head_client) -> None:
    """Ruling 12: `issued_permit` is still `pending_signatures` — nobody has
    signed it yet, so there is no in-force document for a ticket to ride."""
    refused = await head_client.client.post(
        f"/api/v1/permits/{issued_permit.id}/forest-tickets", json=_body(issued_permit)
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "ERR-PERM-001"
    assert refused.json()["error"]["details"]["reason"] == "permit_not_active"


async def test_a_reversed_period_is_refused_before_the_db_ever_sees_it(
    active_permit, head_client
) -> None:
    """`ForestTicketIn`'s own model validator, not the DB CHECK
    (`forest_tickets.period_ordered`) — an inverted period gets a clean 422
    instead of an `IntegrityError` the caller would have to decode."""
    refused = await head_client.client.post(
        f"/api/v1/permits/{active_permit.id}/forest-tickets",
        json=_body(
            active_permit,
            valid_from=str(active_permit.period_to),
            valid_to=str(active_permit.period_from),
        ),
    )
    assert refused.status_code == 422


async def test_a_head_from_another_leshoz_cannot_issue_a_ticket(
    active_permit, other_org_head_client
) -> None:
    """Fix round 1's own lesson (Task 5, `issue_duplicate`), applied here
    first rather than found later: `_assert_organization_in_zone` runs before
    either domain check, so an out-of-zone `permits.manage` holder learns
    nothing about this permit beyond "not yours"."""
    refused = await other_org_head_client.client.post(
        f"/api/v1/permits/{active_permit.id}/forest-tickets", json=_body(active_permit)
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "ERR-ACL-002"


async def test_a_head_from_another_leshoz_cannot_list_tickets(
    active_permit, other_org_head_client
) -> None:
    """The GET route carries the same territorial rule as the POST — it is
    management paperwork, not `list_duplicates`'s wider read audience."""
    refused = await other_org_head_client.client.get(
        f"/api/v1/permits/{active_permit.id}/forest-tickets"
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "ERR-ACL-002"


async def test_an_in_zone_head_can_list_the_register(active_permit, head_client) -> None:
    created = await head_client.client.post(
        f"/api/v1/permits/{active_permit.id}/forest-tickets", json=_body(active_permit)
    )
    assert created.status_code == 201, created.text
    listed = await head_client.client.get(f"/api/v1/permits/{active_permit.id}/forest-tickets")
    assert listed.status_code == 200
    assert [row["number"] for row in listed.json()] == [created.json()["number"]]


def test_the_schema_status_literal_matches_the_tuple_the_check_is_built_from() -> None:
    """The same guard `test_models.py::test_the_schema_literals_match_the_
    tuple_the_check_is_built_from` keeps for `PermitStatus` (lesson: an
    enum-ish column has ONE source of truth — the tuple)."""
    assert set(get_args(ForestTicketStatus)) == set(FOREST_TICKET_STATUSES)
