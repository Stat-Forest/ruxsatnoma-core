"""Service-level lifecycle coverage — direct calls against `service.py` with
plain `User` rows (`make_user`), no HTTP. See `conftest.py`'s own docstring
for why this is the primary coverage shape for this module."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.admin.models import Organization
from app.modules.auth.models import User
from app.modules.gis.models import GisLayer
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.reports import forms_seed, service
from app.modules.reports.models import Report, ReportForm
from tests.modules.auth.test_sessions import make_user
from tests.modules.reports.conftest import make_report_permit, make_signed_act


async def _own_hodim(db: AsyncSession, org: Organization) -> User:
    return await make_user(db, role_code="executor_staff", organization_id=org.id)


async def _own_head(db: AsyncSession, org: Organization) -> User:
    from tests.modules.permits.conftest import unique_pinfl

    return await make_user(
        db, role_code="executor_head", organization_id=org.id, pinfl=unique_pinfl()
    )


# --- report_forms -----------------------------------------------------------


async def test_create_form_rejects_duplicate_code_version(
    db: AsyncSession, central_admin_user: User, grazing_form: ReportForm
):
    with pytest.raises(DomainError) as exc:
        await service.create_form(
            db,
            code=grazing_form.code,
            version=grazing_form.version,
            name={"uz_cyrl": "x"},
            activity_type_id=None,
            period_type="quarter",
            columns=[{"code": "a", "label": {"uz_cyrl": "A"}, "source": "auto", "type": "text"}],
            rules=[],
            schedule={},
            valid_from=None,
            actor=central_admin_user,
        )
    assert exc.value.code == "ERR-REP-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "form_version_exists"


async def test_activate_form_twice_refused(db: AsyncSession, central_admin_user: User):
    form = await service.create_form(
        db,
        code=forms_seed.HAYMAKING_FORM_CODE,
        version=1,
        name={"uz_cyrl": "3-илова"},
        activity_type_id=None,
        period_type="quarter",
        columns=forms_seed.HAYMAKING_COLUMNS,
        rules=[],
        schedule={},
        valid_from=None,
        actor=central_admin_user,
    )
    await service.activate_form(db, form.id, central_admin_user)
    with pytest.raises(DomainError) as exc:
        await service.activate_form(db, form.id, central_admin_user)
    assert exc.value.code == "ERR-REP-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "not_draft"


# --- reports: creation and zone ---------------------------------------------


async def test_create_report_requires_active_form(
    db: AsyncSession, central_admin_user: User, leshoz: Organization, grazing_activity_id
):
    draft_form = await service.create_form(
        db,
        code="draft-only",
        version=1,
        name={"uz_cyrl": "x"},
        activity_type_id=grazing_activity_id,
        period_type="month",
        columns=forms_seed.GRAZING_COLUMNS,
        rules=[],
        schedule={},
        valid_from=None,
        actor=central_admin_user,
    )
    hodim = await _own_hodim(db, leshoz)
    with pytest.raises(DomainError) as exc:
        await service.create_report(
            db,
            form_id=draft_form.id,
            organization_id=leshoz.id,
            period_start=date(2027, 1, 1),
            period_end=date(2027, 1, 31),
            actor=hodim,
        )
    assert exc.value.code == "ERR-REP-003"


async def test_create_report_zone_mismatch_refused(
    db: AsyncSession, grazing_form: ReportForm, leshoz: Organization, other_leshoz: Organization
):
    hodim = await _own_hodim(db, other_leshoz)
    with pytest.raises(DomainError) as exc:
        await service.create_report(
            db,
            form_id=grazing_form.id,
            organization_id=leshoz.id,
            period_start=date(2027, 1, 1),
            period_end=date(2027, 3, 31),
            actor=hodim,
        )
    assert exc.value.code == "ERR-ACL-002"


async def test_create_report_duplicate_period_refused(
    db: AsyncSession, grazing_form: ReportForm, leshoz: Organization
):
    hodim = await _own_hodim(db, leshoz)
    await service.create_report(
        db,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start=date(2027, 1, 1),
        period_end=date(2027, 3, 31),
        actor=hodim,
    )
    with pytest.raises(DomainError) as exc:
        await service.create_report(
            db,
            form_id=grazing_form.id,
            organization_id=leshoz.id,
            period_start=date(2027, 1, 1),
            period_end=date(2027, 3, 31),
            actor=hodim,
        )
    assert exc.value.code == "ERR-REP-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "duplicate_period"


# --- generate / edit / submit ------------------------------------------------


async def test_generate_report_reads_matching_permits(
    db: AsyncSession,
    grazing_form: ReportForm,
    leshoz: Organization,
    contours_layer: GisLayer,
    approval_doc,
    grazing_activity_id: uuid.UUID,
):
    period_from, period_to = date(2027, 5, 1), date(2027, 5, 31)
    await make_report_permit(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
        period_from=period_from,
        period_to=period_to,
        paid_amount=Decimal("2060000.00"),
    )
    hodim = await _own_hodim(db, leshoz)
    report = await service.create_report(
        db,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start=date(2027, 4, 1),
        period_end=date(2027, 6, 30),
        actor=hodim,
    )
    report = await service.generate_report(db, report.id, hodim)
    rows = report.data["rows"]
    assert len(rows) == 1
    assert rows[0]["total_amount"] == "2060000.00"
    assert rows[0]["paid_amount"] == "2060000.00"
    # #105/#106: no acts and no refund on this permit — empty/zero, never
    # `None` printed as text (`render.py` writes the raw value into the
    # cell) and never blank (a reader must be able to tell "checked, zero"
    # from "not computed").
    assert rows[0]["inspection_result"] == ""
    assert rows[0]["refunded_amount"] == "0.00"


async def test_generate_report_lists_every_act_result_in_chronological_order(
    db: AsyncSession,
    grazing_form: ReportForm,
    leshoz: Organization,
    contours_layer: GisLayer,
    approval_doc,
    grazing_activity_id: uuid.UUID,
    default_checklist_id: uuid.UUID,
    vt_01: uuid.UUID,
):
    """Decision #105: every SIGNED act's result, comma-separated, oldest
    first — never "latest" (a corrected May violation must not vanish
    because September was clean) and never "worst" (must not hide that it
    was corrected). Acts are created out of chronological order here on
    purpose, so passing proves the column sorts by `occurred_at`, not by
    insertion order."""
    period_from, period_to = date(2027, 5, 1), date(2027, 5, 31)
    permit = await make_report_permit(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
        period_from=period_from,
        period_to=period_to,
    )
    await make_signed_act(
        db,
        permit=permit,
        org=leshoz,
        checklist_id=default_checklist_id,
        occurred_at=datetime(2027, 5, 25, 9, 0, tzinfo=UTC),
        result="warning",
    )
    await make_signed_act(
        db,
        permit=permit,
        org=leshoz,
        checklist_id=default_checklist_id,
        occurred_at=datetime(2027, 5, 5, 9, 0, tzinfo=UTC),
        result="violation",
        violation_type_item_id=vt_01,
    )
    await make_signed_act(
        db,
        permit=permit,
        org=leshoz,
        checklist_id=default_checklist_id,
        occurred_at=datetime(2027, 5, 15, 9, 0, tzinfo=UTC),
        result="compliant",
    )

    hodim = await _own_hodim(db, leshoz)
    report = await service.create_report(
        db,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start=date(2027, 4, 1),
        period_end=date(2027, 6, 30),
        actor=hodim,
    )
    report = await service.generate_report(db, report.id, hodim)
    rows = report.data["rows"]
    assert len(rows) == 1
    assert rows[0]["inspection_result"] == "violation, compliant, warning"


async def test_generate_report_shows_paid_and_refunded_separately(
    db: AsyncSession,
    grazing_form: ReportForm,
    leshoz: Organization,
    contours_layer: GisLayer,
    approval_doc,
    grazing_activity_id: uuid.UUID,
):
    """Decision #106: a refund never nets against `paid_amount` — a printed
    report for a past month must never change retroactively. Both figures
    are present and independent."""
    period_from, period_to = date(2027, 5, 1), date(2027, 5, 31)
    await make_report_permit(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
        period_from=period_from,
        period_to=period_to,
        paid_amount=Decimal("2060000.00"),
        refunded_amount=Decimal("500000.00"),
    )
    hodim = await _own_hodim(db, leshoz)
    report = await service.create_report(
        db,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start=date(2027, 4, 1),
        period_end=date(2027, 6, 30),
        actor=hodim,
    )
    report = await service.generate_report(db, report.id, hodim)
    rows = report.data["rows"]
    assert len(rows) == 1
    assert rows[0]["paid_amount"] == "2060000.00"
    assert rows[0]["refunded_amount"] == "500000.00"


async def test_generate_report_refused_once_submitted(
    db: AsyncSession, grazing_form: ReportForm, leshoz: Organization
):
    hodim = await _own_hodim(db, leshoz)
    report = await service.create_report(
        db,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start=date(2027, 7, 1),
        period_end=date(2027, 9, 30),
        actor=hodim,
    )
    await service.submit_report(db, report.id, hodim)
    with pytest.raises(DomainError) as exc:
        await service.generate_report(db, report.id, hodim)
    assert exc.value.code == "ERR-REP-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "not_editable"


async def test_submit_refuses_control_ratio_violation(
    db: AsyncSession, grazing_form: ReportForm, leshoz: Organization
):
    hodim = await _own_hodim(db, leshoz)
    report = await service.create_report(
        db,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start=date(2027, 10, 1),
        period_end=date(2027, 12, 31),
        actor=hodim,
    )
    await service.update_report_data(
        db,
        report.id,
        rows=[{"total_amount": "100.00", "paid_amount": "500.00"}],
        actor=hodim,
    )
    with pytest.raises(DomainError) as exc:
        await service.submit_report(db, report.id, hodim)
    assert exc.value.code == "ERR-REP-002"
    assert exc.value.details is not None
    assert exc.value.details["checks"][0]["code"] == "paid_exceeds_total"


# --- sign / return / approve / revise ---------------------------------------


async def test_sign_wrong_organization_refused(
    db: AsyncSession, grazing_form: ReportForm, leshoz: Organization, other_leshoz: Organization
):
    hodim = await _own_hodim(db, leshoz)
    report = await service.create_report(
        db,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start=date(2028, 1, 1),
        period_end=date(2028, 3, 31),
        actor=hodim,
    )
    await service.submit_report(db, report.id, hodim)

    stranger_head = await _own_head(db, other_leshoz)
    assert stranger_head.pinfl is not None
    report_row = await db.get(Report, report.id)
    assert report_row is not None
    document = service._report_bytes(report_row)
    pkcs7 = encode_mock_signature(
        document=document,
        serial=f"SER-{stranger_head.pinfl}",
        issuer="ISS-1",
        pinfl=stranger_head.pinfl,
    )
    with pytest.raises(DomainError) as exc:
        await service.sign_report(db, report.id, pkcs7=pkcs7, actor=stranger_head)
    assert exc.value.code == "ERR-SIGN-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "signer_not_authorized"


async def _approved_report(
    db: AsyncSession,
    *,
    grazing_form: ReportForm,
    leshoz: Organization,
    central_admin_user: User,
    hodim: User,
    head: User,
    period_start,
    period_end,
) -> Report:
    """create -> generate -> submit -> sign -> approve, the shared shape both
    tests below need. Kept as a plain helper rather than a fixture: each
    caller needs its OWN period (the unique constraint) and its own choice of
    whether to `commit()` afterward."""
    report = await service.create_report(
        db,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start=period_start,
        period_end=period_end,
        actor=hodim,
    )
    report = await service.generate_report(db, report.id, hodim)
    report = await service.submit_report(db, report.id, hodim)

    assert head.pinfl is not None
    document = service._report_bytes(report)
    pkcs7 = encode_mock_signature(
        document=document, serial=f"SER-{head.pinfl}", issuer="ISS-1", pinfl=head.pinfl
    )
    report = await service.sign_report(db, report.id, pkcs7=pkcs7, actor=head)
    return await service.approve_report(db, report.id, central_admin_user)


async def test_full_lifecycle_reaches_approved_and_freezes(
    db: AsyncSession, grazing_form: ReportForm, leshoz: Organization, central_admin_user: User
):
    hodim = await _own_hodim(db, leshoz)
    head = await _own_head(db, leshoz)

    report = await _approved_report(
        db,
        grazing_form=grazing_form,
        leshoz=leshoz,
        central_admin_user=central_admin_user,
        hodim=hodim,
        head=head,
        period_start=date(2028, 4, 1),
        period_end=date(2028, 6, 30),
    )
    assert report.status == "approved"
    assert report.approved_by == central_admin_user.id
    await db.commit()  # durable before the illegal statement below

    # The database-level freeze (plan ruling 1): a further UPDATE raises even
    # one issued directly against the row, bypassing the service entirely —
    # the trigger is the backstop `_assert_editable` is only the
    # service-level half of. Plain `text()`, the
    # `tests/modules/norms/test_models.py::
    # test_a_calculation_cannot_be_updated_or_deleted` idiom for this
    # codebase's other append-only triggers: the plpgsql RAISE surfaces as a
    # `DBAPIError` subclass, and matching on the message proves it was OUR
    # trigger. Nothing runs after this in this test — a `db.rollback()`
    # recovery here would EXPIRE the ORM objects above, and re-touching them
    # afterward re-triggers a lazy load this async session cannot service
    # synchronously (`revise_report` gets its own test, starting clean).
    with pytest.raises(DBAPIError, match="frozen"):
        await db.execute(
            text("UPDATE reports SET returned_comment = 'x' WHERE id = :id").bindparams(
                id=report.id
            )
        )


async def test_revise_creates_new_version_from_approved(
    db: AsyncSession, grazing_form: ReportForm, leshoz: Organization, central_admin_user: User
):
    hodim = await _own_hodim(db, leshoz)
    head = await _own_head(db, leshoz)

    approved = await _approved_report(
        db,
        grazing_form=grazing_form,
        leshoz=leshoz,
        central_admin_user=central_admin_user,
        hodim=hodim,
        head=head,
        period_start=date(2028, 7, 1),
        period_end=date(2028, 9, 30),
    )

    revision = await service.revise_report(db, approved.id, hodim)
    assert revision.id != approved.id
    assert revision.version_no == 2
    assert revision.parent_report_id == approved.id
    assert revision.status == "created"
    assert revision.data == approved.data


async def test_return_then_resubmit(
    db: AsyncSession, grazing_form: ReportForm, leshoz: Organization
):
    hodim = await _own_hodim(db, leshoz)
    head = await _own_head(db, leshoz)

    report = await service.create_report(
        db,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start=date(2028, 7, 1),
        period_end=date(2028, 9, 30),
        actor=hodim,
    )
    await service.submit_report(db, report.id, hodim)

    report = await service.return_report(db, report.id, comment="fix column 16", actor=head)
    assert report.status == "returned"
    assert report.returned_by == "head"
    assert report.returned_comment == "fix column 16"

    # Mutable again while returned.
    report = await service.update_report_data(db, report.id, rows=[], actor=hodim)
    report = await service.submit_report(db, report.id, hodim)
    assert report.status == "submitted"
    assert report.returned_by is None


async def _head_approved_report(
    db: AsyncSession,
    *,
    grazing_form: ReportForm,
    leshoz: Organization,
    hodim: User,
    head: User,
    period_start,
    period_end,
) -> Report:
    """create -> generate -> submit -> sign — one step short of
    `_approved_report`, the `head_approved` state `return_report`'s
    central-office branch needs."""
    report = await service.create_report(
        db,
        form_id=grazing_form.id,
        organization_id=leshoz.id,
        period_start=period_start,
        period_end=period_end,
        actor=hodim,
    )
    report = await service.generate_report(db, report.id, hodim)
    report = await service.submit_report(db, report.id, hodim)

    assert head.pinfl is not None
    document = service._report_bytes(report)
    pkcs7 = encode_mock_signature(
        document=document, serial=f"SER-{head.pinfl}", issuer="ISS-1", pinfl=head.pinfl
    )
    return await service.sign_report(db, report.id, pkcs7=pkcs7, actor=head)


async def test_return_head_approved_refuses_reports_sign_only_actor(
    db: AsyncSession,
    grazing_form: ReportForm,
    leshoz: Organization,
    other_leshoz: Organization,
):
    """The ROUTE gates `/return` on `require_any_permission(reports.sign,
    reports.accept)` because the `submitted` branch legitimately needs
    `reports.sign` (`_assert_report_signer`'s identity check). This proves
    the `head_approved` branch is not fooled by that `any` admission: an
    executor_head from a DIFFERENT organization holds `reports.sign` by role
    and passes the route, but holds no `reports.accept` and must be refused
    here — with `ERR-ACL-001`, not a bare 403."""
    hodim = await _own_hodim(db, leshoz)
    head = await _own_head(db, leshoz)
    report = await _head_approved_report(
        db,
        grazing_form=grazing_form,
        leshoz=leshoz,
        hodim=hodim,
        head=head,
        period_start=date(2029, 1, 1),
        period_end=date(2029, 3, 31),
    )

    stranger_head = await _own_head(db, other_leshoz)
    with pytest.raises(DomainError) as exc:
        await service.return_report(db, report.id, comment="not yours", actor=stranger_head)
    assert exc.value.code == "ERR-ACL-001"
    assert exc.value.details is not None
    assert exc.value.details["permission"] == "reports.accept"

    # Refused, not returned: the report itself is untouched.
    report_row = await db.get(Report, report.id)
    assert report_row is not None
    assert report_row.status == "head_approved"
    assert report_row.returned_by is None


async def test_return_head_approved_by_reports_accept_holder(
    db: AsyncSession,
    grazing_form: ReportForm,
    leshoz: Organization,
    central_admin_user: User,
):
    """The legitimate path: a `reports.accept` holder (central office) still
    returns a `head_approved` report, recorded as a return by the centre."""
    hodim = await _own_hodim(db, leshoz)
    head = await _own_head(db, leshoz)
    report = await _head_approved_report(
        db,
        grazing_form=grazing_form,
        leshoz=leshoz,
        hodim=hodim,
        head=head,
        period_start=date(2029, 4, 1),
        period_end=date(2029, 6, 30),
    )

    returned = await service.return_report(
        db, report.id, comment="needs a correction", actor=central_admin_user
    )
    assert returned.status == "returned"
    assert returned.returned_by == "center"
    assert returned.returned_comment == "needs a correction"
