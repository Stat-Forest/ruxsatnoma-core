"""`POST /applications/{id}/return` (plan `03.9b-applications-review` task 3):
send an application back to the applicant for correction, with a typed RJ-*
reason, the fields to fix, and a legal basis.

`fields_to_fix` is a JSON OBJECT, not a list (corrected 2026-09-05): field
name -> what is wrong with it, `ApplicationStatusHistory.fields_to_fix` /
`TimelineHistoryRow.fields_to_fix` are both `dict[str, Any] | None`."""


async def test_a_return_requires_a_reason_of_the_right_type(
    hodim_client, application_in_review, rj_01_return_reason, rj_03_reject_reason
) -> None:
    """Ruling 3: tz/10 types each RJ code. RJ-03 ('outside the forest fund') is
    a REFUSAL — returning under it would misdescribe the decision, and the
    applicant would be told to fix something unfixable."""
    wrong = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/return",
        json={
            "reason_item_id": str(rj_03_reject_reason.id),
            "fields_to_fix": {"period_from": "начало вне сезона"},
            "legal_basis": "ВМҚ 290",
        },
    )
    assert wrong.status_code == 422
    assert wrong.json()["error"]["details"]["reason"] == "reason_not_returnable"

    right = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/return",
        json={
            "reason_item_id": str(rj_01_return_reason.id),
            "fields_to_fix": {"period_from": "начало вне сезона"},
            "legal_basis": "ВМҚ 290",
        },
    )
    assert right.status_code == 200, right.text
    assert right.json()["status"] == "RETURNED"


async def test_a_return_without_a_legal_basis_is_refused(
    hodim_client, application_in_review, rj_01_return_reason
) -> None:
    result = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/return",
        json={
            "reason_item_id": str(rj_01_return_reason.id),
            "fields_to_fix": {"period_from": "начало вне сезона"},
        },
    )
    assert result.status_code == 422


async def test_the_return_reason_reaches_the_timeline(
    hodim_client, applicant_client, application_in_review, rj_01_return_reason
) -> None:
    await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/return",
        json={
            "reason_item_id": str(rj_01_return_reason.id),
            "fields_to_fix": {
                "period_from": "начало вне сезона",
                "period_to": "срок выходит за сезон выпаса",
            },
            "legal_basis": "ВМҚ 290",
        },
    )
    timeline = (
        await applicant_client.get(f"/api/v1/applications/{application_in_review}/timeline")
    ).json()
    last = timeline["status_history"][-1]
    assert last["to_status"] == "RETURNED"
    assert last["fields_to_fix"] == {
        "period_from": "начало вне сезона",
        "period_to": "срок выходит за сезон выпаса",
    }
    assert last["legal_basis"] == "ВМҚ 290"


async def test_a_returned_application_is_edited_and_resubmitted_keeping_its_number(
    hodim_client, applicant_client, application_in_review, rj_01_return_reason
) -> None:
    """Ruling 14: same application, same number, same deadline — but the checks
    run again, a new calculation is written and the package is signed afresh.

    The assignee is unaffected by this same resubmission — Task 1's
    `test_a_resubmission_does_not_re_fire_auto_assignment` already covers
    that guard (ruling 6) directly against `submit`, so this test does not
    repeat the assertion."""
    from tests.modules.applications.test_submit import _submit

    before = (await applicant_client.get(f"/api/v1/applications/{application_in_review}")).json()

    await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/return",
        json={
            "reason_item_id": str(rj_01_return_reason.id),
            "fields_to_fix": {"period_to": "срок выходит за сезон выпаса"},
            "legal_basis": "ВМҚ 290",
        },
    )
    patched = await applicant_client.patch(
        f"/api/v1/applications/{application_in_review}", json={"period_to": "2027-08-31"}
    )
    assert patched.status_code == 200, "a RETURNED application must be editable again"

    again = await _submit(applicant_client, application_in_review)
    assert again.status_code == 200, again.text

    after = again.json()
    assert after["number"] == before["number"]
    assert after["sla_deadline_at"] == before["sla_deadline_at"]

    # The submission signatures live PER HISTORY ROW, not at the top level:
    # `timeline["signatures"]` is the DECISION signature (`service.py:2098`,
    # `schemas.py:544`) and is empty here. Ruling 25 gave each submission
    # attempt its own signed object, keyed by its SUBMITTED history row's id
    # (`service.py:2085-2093`) — so "signed twice" means two SUBMITTED rows
    # each carrying one signature, which is also what proves they are two
    # distinct signatures over two distinct packages.
    timeline = (
        await applicant_client.get(f"/api/v1/applications/{application_in_review}/timeline")
    ).json()
    submissions = [r for r in timeline["status_history"] if r["to_status"] == "SUBMITTED"]
    assert len(submissions) == 2, "the resubmission adds its own history row"
    assert all(len(r["signatures"]) == 1 for r in submissions), "each package is signed"
    assert timeline["signatures"] == [], "the top level is the DECISION signature"


async def test_a_return_that_moves_the_contour_to_another_leshoz_reassigns_the_organization(
    hodim_client,
    applicant_client,
    other_zone_hodim_client,
    application_in_review,
    rj_01_return_reason,
    other_leshoz_published_contour,
    other_leshoz_grazing_norm,
    leshoz,
    other_leshoz,
) -> None:
    """Final whole-branch review, CRITICAL. RJ-01 plus `fields_to_fix:
    {contour_id}` is exactly "wrong plot, pick the right one" — the applicant
    then PATCHes `contour_id` onto a plot owned by a DIFFERENT leshoz and
    resubmits. Before the fix, `_auto_assign_on_submission` saw an existing
    active assignment and returned unconditionally (ruling 6's guard, read too
    broadly): `assigned_org_id` stayed the FIRST leshoz's forever, because
    `_effective_organization` echoes a set `assigned_org_id` back verbatim
    rather than re-checking the contour. The application then stayed in the
    first leshoz's queue while pointing at the second leshoz's plot — the
    wrong authority reviews, approves and would ERI-sign a permit for land it
    does not own, while the actual owner cannot even see the file.

    The fix re-derives the organization from the contour itself whenever an
    active assignment already exists, and only when that disagrees with what
    is stored does it drop the stale assignment and pick again — which is
    exactly what this test proves end to end."""
    from tests.modules.applications.test_submit import _submit

    returned = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/return",
        json={
            "reason_item_id": str(rj_01_return_reason.id),
            "fields_to_fix": {"contour_id": "неверный участок — территория другого лесхоза"},
            "legal_basis": "ВМҚ 290",
        },
    )
    assert returned.status_code == 200, returned.text

    patched = await applicant_client.patch(
        f"/api/v1/applications/{application_in_review}",
        json={"contour_id": str(other_leshoz_published_contour.id)},
    )
    assert patched.status_code == 200, patched.text

    again = await _submit(applicant_client, application_in_review)
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "SUBMITTED"

    timeline = (
        await applicant_client.get(f"/api/v1/applications/{application_in_review}/timeline")
    ).json()
    active = [a for a in timeline["assignments"] if a["is_active"]]
    assert len(active) == 1, "the partial unique index allows exactly one active row"
    assert active[0]["org_id"] == str(other_leshoz.id), (
        "the application must move to the leshoz that now owns its plot"
    )

    # Leshoz A no longer has this application in its territory — it is not an
    # existence oracle, so the refusal is 404, the same answer a stranger's id
    # gets (`_readable_application`'s own rule).
    stale = await hodim_client.get(f"/api/v1/applications/{application_in_review}")
    assert stale.status_code == 404

    # Leshoz B — the plot's actual owner — can now open its own application.
    fresh = await other_zone_hodim_client.get(f"/api/v1/applications/{application_in_review}")
    assert fresh.status_code == 200, fresh.text


async def test_a_return_resubmitted_on_the_same_contour_keeps_its_reviewer(
    hodim_client, applicant_client, application_in_review, rj_01_return_reason, leshoz, hodim_user
) -> None:
    """The other half of the same fix: a correction that does NOT touch
    `contour_id` must not be mistaken for one that does. Task 1's
    `test_a_resubmission_does_not_re_fire_auto_assignment` already pins this
    directly against `submit` (ruling 6); this is the same guarantee proven
    through the actual `/return` route the final review's Critical walks
    through, so the fix is checked against both the changed and the unchanged
    path."""
    from tests.modules.applications.test_submit import _submit

    returned = await hodim_client.post(
        f"/api/v1/applications/{application_in_review}/return",
        json={
            "reason_item_id": str(rj_01_return_reason.id),
            "fields_to_fix": {"period_to": "срок выходит за сезон выпаса"},
            "legal_basis": "ВМҚ 290",
        },
    )
    assert returned.status_code == 200, returned.text

    patched = await applicant_client.patch(
        f"/api/v1/applications/{application_in_review}", json={"period_to": "2027-08-31"}
    )
    assert patched.status_code == 200, patched.text

    again = await _submit(applicant_client, application_in_review)
    assert again.status_code == 200, again.text

    timeline = (
        await applicant_client.get(f"/api/v1/applications/{application_in_review}/timeline")
    ).json()
    active = [a for a in timeline["assignments"] if a["is_active"]]
    assert len(active) == 1, "an unchanged contour must not insert a second assignment row"
    assert active[0]["org_id"] == str(leshoz.id)
    assert active[0]["user_id"] == str(hodim_user.id), (
        "a resubmission on the SAME contour must keep its existing reviewer"
    )
