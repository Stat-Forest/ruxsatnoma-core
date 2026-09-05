"""Conclusions and recalculation (plan 03.9b task 5).

`POST /applications/{id}/conclusion` records a specialist's written finding —
`kind="executor"` (the hodim, `applications.review`) or `kind="gis"` (the GIS
specialist, `applications.conclude_gis`) — on the record for the head to read
before deciding (tz/04 С8). Conclusions are immutable (ruling 10): a
correction is a new row, never an edit.

`POST /applications/{id}/recalculate` writes a new `calculations` row through
`norms.service.save_calculation`, which already carries its own actor- and
status-dependent guard (`_assert_application_open_for_calculation`) — this
route re-implements none of it (ruling 17).

**`kind="gis"` was fail-closed until fix round 1.** The first draft of this
task found no code in `gis`'s own registry (`app/modules/gis/permissions.py`
registers only `gis.contours.manage`, `.approve` and `gis.layers.manage`,
none of them a fit) and refused `kind="gis"` for every caller rather than
widen one, flagging the gap for `decisions.md`/`design/03` to close. The
controller ruling closed it: `applications.conclude_gis` is now a real code
(owned by `applications`, the same reason `review`/`decide`/`assign` are
too — the thing it authorises is a write on an APPLICATION), granted to
`gis_specialist` by migration `0025` (amended, not a new revision — the
branch is unmerged and this stage's own house rule is one revision per
stage). `gis.contours.approve` was rejected as a stand-in: it would let a
pure contour editor write conclusions on applications, a different
authority. `test_both_specialists_put_their_conclusions_on_the_record` below
is the brief's own Step-1 test, restored now that the route is reachable;
`test_a_plain_hodim_cannot_write_a_gis_conclusion` pins that the new code
actually gates — holding `applications.review` alone is not enough.
"""

import uuid

from sqlalchemy import func, select

from app.modules.applications import service
from app.modules.norms.models import Calculation


async def test_both_specialists_put_their_conclusions_on_the_record(
    hodim_client, gis_specialist_client, executor_head_client, application_in_review
) -> None:
    """tz/04 С8: the head sees both conclusions before deciding."""
    a = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/conclusion",
        json={"text": "Комплект полный", "kind": "executor", "recommendation": "approve"},
    )
    assert a.status_code == 201, a.text

    b = await gis_specialist_client.post(
        f"/api/v1/applications/{application_in_review}/conclusion",
        json={"text": "Контур свободен", "kind": "gis", "recommendation": "approve"},
    )
    assert b.status_code == 201, b.text

    card = (await executor_head_client.get(f"/api/v1/applications/{application_in_review}")).json()
    assert {c["kind"] for c in card["conclusions"]} == {"executor", "gis"}


async def test_a_plain_hodim_cannot_write_a_gis_conclusion(
    hodim_client, application_in_review
) -> None:
    """`hodim_client` holds `applications.review` — enough for `kind="executor"`
    (proven above) but nothing else. `applications.conclude_gis` is its own
    code precisely so holding the OTHER one does not also open this branch;
    resolving by code, never by "any staff permission will do"."""
    result = await hodim_client.post(
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
    """Owner decision, 2026-09-05, unmoved by fix round 1: `/recalculate`
    stays narrowed to "hodim or head" even now that the GIS specialist has a
    real permission for their OWN route — `applications.conclude_gis` is not
    in `_APPLICATION_RECALCULATE_CODES` and must not be added there. The GIS
    specialist writes their `kind="gis"` conclusion instead (design/03),
    never a re-price. The route-level `require_any_permission(APPLICATIONS_
    REVIEW, APPLICATIONS_DECIDE)` answers this with a legible 403 before
    `norms.service.save_calculation` is ever reached — never the confusing
    404 an unguarded route would give a non-entitled caller."""
    result = await gis_specialist_client.post(
        f"/api/v1/applications/{application_in_review}/recalculate"
    )
    assert result.status_code == 403, result.text
    assert result.json()["error"]["code"] == "ERR-ACL-001"
