"""Conclusions and recalculation (plan 03.9b task 5).

`POST /applications/{id}/conclusion` records a specialist's written finding —
`kind="executor"` (the hodim, `applications.review`) or `kind="gis"` — on the
record for the head to read before deciding (tz/04 С8). Conclusions are
immutable (ruling 10): a correction is a new row, never an edit.

`POST /applications/{id}/recalculate` writes a new `calculations` row through
`norms.service.save_calculation`, which already carries its own actor- and
status-dependent guard (`_assert_application_open_for_calculation`) — this
route re-implements none of it (ruling 17).

**`kind="gis"` is refused today, not merely untested.** Task 5's own brief
promised the route to "hodim or the GIS specialist", but
`app/modules/gis/permissions.py` registers no code that means "authorised to
write an application conclusion" — only `gis.contours.manage`,
`gis.contours.approve` and `gis.layers.manage` (migration 0010's
`ROLE_GRANTS` for `gis_specialist`), and none of the three fits. Per the
brief's own instruction, this fails CLOSED — `ERR-ACL-001` for every caller,
including the real `gis_specialist` role holding both of its own grants
(`gis_specialist_client` below) — rather than being gated on a widened
`applications.*` code invented for this route. `test_a_gis_conclusion_is_
refused_pending_a_permission_code` pins that, in place of the brief's own
`test_both_specialists_put_their_conclusions_on_the_record`, whose `kind="gis"`
half assumed a permission that does not exist. This is a gap for
`decisions.md`/`design/03` to close explicitly with a real code, not something
this test suite can paper over.
"""

import uuid

from sqlalchemy import func, select

from app.modules.applications import service
from app.modules.norms.models import Calculation


async def test_an_executor_conclusion_is_recorded(
    hodim_client, executor_head_client, application_in_review
) -> None:
    """tz/04 С8: the head sees the hodim's conclusion before deciding."""
    result = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/conclusion",
        json={"text": "Комплект полный", "kind": "executor", "recommendation": "approve"},
    )
    assert result.status_code == 201, result.text

    card = (await executor_head_client.get(f"/api/v1/applications/{application_in_review}")).json()
    assert {c["kind"] for c in card["conclusions"]} == {"executor"}
    assert card["conclusions"][0]["text"] == "Комплект полный"
    assert card["conclusions"][0]["recommendation"] == "approve"


async def test_a_gis_conclusion_is_refused_pending_a_permission_code(
    gis_specialist_client, application_in_review
) -> None:
    """`gis_specialist_client` is a real `gis_specialist` ROLE user, holding
    every grant migrations 0010/0011 actually give that role (`gis.contours.
    manage`, `gis.layers.manage`, `norms.manage` — and, notably, NOT
    `applications.review`) — proving the gap is in the REGISTRY, not in one
    fixture's grant list. `design/03` grants the GIS specialist their own
    conclusion, but no permission code exists yet to gate it, so
    `service.add_conclusion` fails closed rather than widening an
    `applications.*` code invented for this route."""
    result = await gis_specialist_client.post(
        f"/api/v1/applications/{application_in_review}/conclusion",
        json={"text": "Контур свободен", "kind": "gis", "recommendation": "approve"},
    )
    assert result.status_code == 403, result.text
    assert result.json()["error"]["code"] == "ERR-ACL-001"


async def test_a_conclusion_cannot_be_edited_or_deleted(
    hodim_client, application_in_review
) -> None:
    """Ruling 10: the head signs on the basis of what the conclusions said. An
    editable conclusion makes that signature unverifiable."""
    created = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/conclusion",
        json={"text": "первая версия", "kind": "executor", "recommendation": "approve"},
    )
    cid = created.json()["id"]

    # No route is ever registered at this path — only `POST .../conclusion`
    # (no `/{cid}` segment) exists. A PATCH/DELETE against an unmatched path
    # template is Starlette 404, not 405: 405 only applies when the path
    # matches a registered route and the method does not.
    assert (
        await hodim_client.patch(
            f"/api/v1/applications/{application_in_review}/conclusion/{cid}",
            json={"text": "исправил"},
        )
    ).status_code == 404
    assert (
        await hodim_client.delete(f"/api/v1/applications/{application_in_review}/conclusion/{cid}")
    ).status_code == 404


async def test_a_corrected_conclusion_is_a_second_row_and_both_are_visible(
    hodim_client, executor_head_client, application_in_review
) -> None:
    for text in ("первая версия", "уточнение после выезда"):
        await hodim_client.post(
            f"/api/v1/applications/{application_in_review}/conclusion",
            json={"text": text, "kind": "executor", "recommendation": "approve"},
        )
    card = (await executor_head_client.get(f"/api/v1/applications/{application_in_review}")).json()
    executor = [c for c in card["conclusions"] if c["kind"] == "executor"]
    assert len(executor) == 2
    assert [c["text"] for c in executor] == ["первая версия", "уточнение после выезда"]


async def test_recalculation_writes_a_new_row_and_the_newest_wins(
    db, hodim_client, application_in_review
) -> None:
    """Ruling 11 + tz/05 invariant 4. 3.10 bills `current_calculation`, so the
    invoice follows a recalculation with no change on its side."""
    first = (await hodim_client.get(f"/api/v1/applications/{application_in_review}")).json()[
        "calculation"
    ]["id"]

    result = await hodim_client.post(f"/api/v1/applications/{application_in_review}/recalculate")
    assert result.status_code == 200, result.text

    rows = await db.scalar(
        select(func.count())
        .select_from(Calculation)
        .where(Calculation.application_id == uuid.UUID(application_in_review))
    )
    assert rows == 2
    newest = await service.current_calculation(db, uuid.UUID(application_in_review))
    assert newest is not None
    assert str(newest.id) != first


async def test_recalculating_an_approved_application_is_refused(
    hodim_client, approved_application
) -> None:
    """Ruling 17: 3.10a bills `current_calculation` and 3.11a prints it into
    form 1-ilova field 18. A recalculation after approval makes the permit
    state a sum the citizen never paid.

    The refusal comes from `norms`, so it is that module's own state-conflict
    code — `ERR-NORM-005` (`norms/service.py:1088`), not `ERR-APP-004`."""
    result = await hodim_client.post(
        f"/api/v1/applications/{approved_application}/recalculate", json={}
    )
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "ERR-NORM-005"


async def test_the_gis_specialist_cannot_recalculate(
    gis_specialist_client, application_in_review
) -> None:
    """Owner decision, 2026-09-05: `/recalculate` is narrowed to "hodim or
    head" — the GIS specialist writes their own `kind="gis"` conclusion
    instead (design/03), never a re-price. The route-level
    `require_any_permission(APPLICATIONS_REVIEW, APPLICATIONS_DECIDE)` answers
    this with a legible 403 before `norms.service.save_calculation` is ever
    reached — never the confusing 404 an unguarded route would give a
    non-entitled caller."""
    result = await gis_specialist_client.post(
        f"/api/v1/applications/{application_in_review}/recalculate"
    )
    assert result.status_code == 403, result.text
    assert result.json()["error"]["code"] == "ERR-ACL-001"
