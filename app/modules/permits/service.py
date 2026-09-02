"""Permits service — issuance, and the in-process reads a permit's document needs.

Issuance is the point of the whole system: a PAID application becomes a numbered,
rendered, hash-frozen document. Three invariants govern this file.

**The snapshot is immutable** (`tz/05` invariant 7, ruling 14). Every field the PDF
shows is copied into `permits.snapshot` at issuance and read from there forever
after. An applicant who renames themselves tomorrow does not change a permit issued
today, and a permit re-rendered from its own snapshot years later — after the tariff
has changed twice — is the same document.

**The document is rendered once and its hash frozen** (ruling 3). `doc_hash` is
`sha256` of the stored bytes, and all four ERI signatures are taken over exactly
those bytes, which is what makes `signatures.service.require_complete` mean what it
says. A corrected permit is a revocation plus a new permit, never a re-render.

**`permits` may not read `payments`** (`design/01` rule 3 — both are level 4 and
neither may call the other). The application reaching `PAID` is 3.10a's job and is
the only fact this module needs; the check here is on the application's own status
and never on an invoice.
"""

import asyncio
import hashlib
import secrets
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import files, storage
from app.core.abac import Zone, zone_of
from app.core.errors import err
from app.core.models import MediaFile
from app.core.time import business_today
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import Organization
from app.modules.applications import service as applications_service
from app.modules.audit import service as audit
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.notifications import service as notifications
from app.modules.permits import events, render, repo
from app.modules.permits.models import Permit, PermitStatusHistory, PermitTemplate

# Audit action codes: "<object>.<verb>" in English, and the constant lives with the
# acting module — audit is level 0 and knows no domain vocabulary (decision #38,
# ruling 17). The dot is shared with `notification_templates.event_code`
# (`permit.issued`) and neither is a bus name; see `permits/events.py`.
PERMIT_ISSUE = "permit.issue"

# The permit's initial status. `active` is Task 4's, when the last signature lands.
INITIAL_STATUS = "pending_signatures"

# `tz/13` field 19 is «Статус оплаты и дата». The STATUS is what this module can
# state on its own authority — issuance runs from `PAID` and from nothing else, so
# the word is a constant rather than a lookup. The DATE half is not stored here and
# is not on the document: it lives in `payments`, which this module may not read
# (design/01 rule 3), and `applications.service` exposes no paid-at. Printing the
# issuance date in its place would put a wrong date on a legal document.
PAYMENT_STATUS_PAID = "Тўланган"

# The document's language. `tz/13`'s note: «на государственном языке» — the permit
# is issued in Uzbek Cyrillic, whatever language the holder reads the cabinet in.
# `organizations.name`/`activity_types.name` are JSONB with this key.
DOCUMENT_LANGUAGE = "uz_cyrl"


def _organization_in_zone(zone: Zone, org: Organization) -> bool:
    """Per-row equivalent of `zone_filter`'s SQL for ONE organization row — a LOCAL
    copy of the private helper of the same name and identical logic in
    `gis.service` and `norms.service`. The module boundary (cross-module calls go
    through the other module's service) rules out importing either: it is not part
    of their declared public surface."""
    if zone.region_id is not None and zone.region_id != org.region_id:
        return False
    if zone.district_id is not None and zone.district_id != org.district_id:
        return False
    if zone.organization_id is not None and zone.organization_id != org.id:
        return False
    return True


async def _assert_in_zone(db: AsyncSession, actor: User, contour_id: uuid.UUID) -> uuid.UUID:
    """Which leshoz issues this permit, refusing an actor whose zone does not
    cover it. Returns that organization id, so the caller never resolves it twice.

    ALL THREE axes of `app/core/abac.py`'s `Zone`, not `organization_id` alone:
    narrowing it to one was a finding of 3.6a's own final review — an actor with a
    region but no organization of their own then passes for every organization in
    the country, and that shape is creatable today
    (`admin.users_service.create_user` sets the three columns independently).

    Separate from the route's `permits.issue` check, which answers whether this
    role may issue AT ALL (lesson: zone scoping is not a permission check). The
    organization is resolved through `gis.service` — never `gis.repo`, never a
    direct query of `contours` — exactly as `norms.service._assert_norm_zone` does.
    """
    organization_id = await gis_service.contour_organization(db, contour_id)
    if organization_id is None:
        raise err("ERR-SYS-003", details={"contour": str(contour_id)})
    zone = zone_of(actor)
    if zone != Zone(None, None, None):
        org = await admin_repo.get_organization(db, organization_id)
        if org is None or not _organization_in_zone(zone, org):
            raise err("ERR-ACL-002")
    return organization_id


def _required[T](value: T | None, *, field: str) -> T:
    """A permit is a legal document: an unfilled requisite is a defect that must
    fail HERE, loudly and by name, rather than reach a citizen as a blank line or
    the word "None" (the renderer refuses it too — this says WHICH source was
    empty, which the renderer cannot know).

    Generic rather than `Any` so it also NARROWS: an `Application` is autosaved
    field by field and half of its columns are nullable (3.9a ruling 7), so every
    one of them reaches here as `T | None` and pyright checks that nothing skips
    this call on the way into the permit row."""
    if value is None or value == "":
        raise err("ERR-VAL-001", details={"reason": "missing_requisite", "field": field})
    return value


def _localized(name: Any, *, field: str) -> str:
    """A JSONB `{uz_cyrl: ..., ru: ...}` reference name, in the document's own
    language. No fallback to `ru`: a Russian leshoz name on an Uzbek-language
    permit is a defect that should be fixed in the classifier, not papered over."""
    if not isinstance(name, dict):
        raise err("ERR-VAL-001", details={"reason": "missing_requisite", "field": field})
    return str(_required(name.get(DOCUMENT_LANGUAGE), field=field))


def _money(value: Decimal | None) -> str | None:
    """A fixed-scale NUMERIC as the document prints it. `format(value, "f")` and
    never `str(Decimal)`, which can produce scientific notation."""
    return None if value is None else format(value, "f")


def qr_url(token: str) -> str:
    """The address the printed QR points at (`design/03` § public). Built from
    `settings.public_base_url`, which must be the externally reachable origin — a
    permit is printed once and the URL on it cannot be corrected afterwards.

    Kept short on purpose: the QR's module size shrinks as the payload grows, and
    the bundled layout prints the symbol at 28 mm (task 2's inherited caveat)."""
    return f"{get_settings().public_base_url.rstrip('/')}/api/v1/public/permits/check?qr={token}"


async def _layout_html(db: AsyncSession, template: PermitTemplate) -> str:
    """The layout this template means.

    `layout_file_id` NULL means "the layout bundled with the module"
    (`app/modules/permits/assets/default_layout.html`, task 1 decision 2): a
    migration cannot put bytes in MinIO, and a row pointing at a storage key that
    does not exist would be worse than an honest null. A non-null value is an
    administrator's own uploaded layout and wins from then on.
    """
    if template.layout_file_id is None:
        return render.default_layout()
    file = await db.get(MediaFile, template.layout_file_id)
    if file is None or file.status != "active":
        raise err(
            "ERR-VAL-001",
            details={"reason": "layout_file_missing", "file_id": str(template.layout_file_id)},
        )
    data = await storage.get_object(file.storage_key)
    return data.decode("utf-8")


async def _snapshot(
    db: AsyncSession,
    *,
    series: str,
    number: int,
    applicant_id: uuid.UUID,
    activity_type_id: uuid.UUID,
    organization_id: uuid.UUID,
    contour_id: uuid.UUID,
    area_ha: Decimal,
    period_from: date,
    period_to: date,
    amount: Decimal,
    sb_load: Decimal | None,
    calculation_id: uuid.UUID,
) -> dict[str, Any]:
    """Form 1-ilova's requisites (`tz/13` § 1-илова, ruling 14), gathered once and
    never read from their sources again.

    Everything is already a string: the snapshot is what the renderer receives, so
    the PDF and the stored record cannot disagree, and JSONB has no `Decimal` or
    `date` (lesson: nothing in this app configures a JSON encoder — coerce at the
    boundary). Two of `tz/13`'s 25 are deliberately absent: №24 «печать
    подлинности» IS the QR, and №25 «статус документа» is `permits.status`, which
    changes over the permit's life and must not be frozen into an immutable
    snapshot.

    `calculation_id` is not printed. It is here so the permit can be compared
    against the invoice that was actually paid (ruling 19): 3.9b's `recalculate`
    writes a new calculation row and "the newest wins", so without the id a permit
    could carry a figure the citizen never paid and nothing on this side would show
    it. A guard on another branch is not evidence.

    Every value arrives already validated and non-null: half of `applications`'
    columns are nullable because a DRAFT is autosaved field by field (3.9a ruling
    7), and `issue` resolves each through `_required` before calling this — so a
    missing requisite is named at its SOURCE rather than reaching the renderer as
    an unfilled placeholder it cannot attribute.
    """
    applicant = await auth_service.get_applicant(db, applicant_id)
    if applicant is None:
        raise err("ERR-VAL-001", details={"reason": "missing_requisite", "field": "applicant"})
    organization = await admin_repo.get_organization(db, organization_id)
    if organization is None:
        raise err("ERR-VAL-001", details={"reason": "missing_requisite", "field": "organization"})
    activity = await admin_repo.get_activity_type(db, activity_type_id)
    if activity is None:
        raise err("ERR-VAL-001", details={"reason": "missing_requisite", "field": "activity_type"})
    contour_number = await gis_service.contour_number(db, contour_id)

    return {
        # 1-2: the issuing body, the series and the number
        "organization_name": _localized(organization.name, field="organization_name"),
        "series": series,
        # Six digits — `design/03`'s «серия А № 000123», and the reason the API
        # returns the integer while the document shows the padded form.
        "number": f"{number:06d}",
        # 3: «Дата выдачи» — the calendar date the DOCUMENT bears, in Tashkent
        # (`business_today`, never `date.today()`: on a UTC container the server's
        # own date is yesterday for ~5 hours a day — lesson). Distinct from
        # `permits.issued_at`, the timestamp Task 4 sets when the last signature
        # makes the permit legally in force.
        "issued_at": business_today().isoformat(),
        # 8-10: the plot, the holder
        "activity_name": _localized(activity.name, field="activity_name"),
        "holder_name": str(_required(applicant.name, field="holder_name")),
        # An individual is identified by PINFL, a legal entity by STIR —
        # `identity_by_kind` (migration 0003) guarantees exactly one is set.
        "holder_pinfl": str(_required(applicant.pinfl or applicant.stir, field="holder_pinfl")),
        "contour_number": str(_required(contour_number, field="contour_number")),
        "area_ha": _money(area_ha),
        # 16-19: the load, the term, the money, the payment
        "sb_load": _money(sb_load),
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
        "amount": _money(amount),
        "payment_status": PAYMENT_STATUS_PAID,
        # Not printed: the link back to the calculation this amount came from.
        "calculation_id": str(calculation_id),
    }


async def issue(db: AsyncSession, application_id: uuid.UUID, *, actor: User) -> Permit:
    """Form the permit for a PAID application, in one transaction.

    The order below is load-bearing. In particular the counter is touched only
    after every input has been gathered: a series number handed out to a failed
    issuance is a gap in a legal register that nobody can explain years later, and
    `permit_counters` has no way to give one back.

    The application does NOT move to `PERMIT_ISSUED` here (ruling 18). `tz/05`
    defines that status as «сформировано **и подписано**», and four different
    people have yet to sign; Task 4 moves it when the last signature lands. Between
    paying and that moment an applicant's cabinet honestly shows «оплачено» with a
    permit attached and awaiting signatures.
    """
    application = await applications_service.get(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})

    # 1. PAID, and nothing else (ruling 10, tz/04 С11). A refused attempt is
    # RI-10, CRITICAL and immediate in tz/10 — written to the audit journal and
    # COMMITTED before the exception that explains it, because a raise rolls the
    # trail back with it (the early-commit-on-denial pattern, decision #40).
    if application.status != "PAID":
        await audit.log(
            db,
            action=PERMIT_ISSUE,
            user_id=actor.id,
            object_type="application",
            object_id=application.id,
            result="denied",
            basis="application is not PAID",
            new_value={"status": application.status},
            extra={"risk_indicator": "RI-10"},
        )
        await db.commit()
        raise err("ERR-PAY-001", details={"status": application.status})

    # 2. One permit per application (`permits.application_id` is unique). Caught
    # here as a domain answer rather than left to the constraint, which would
    # surface as an IntegrityError 500 the applicant reads as "issuance failed".
    # This is also why the route needs no `Idempotency-Key`: a replayed request is
    # refused with ERR-PERM-001, never answered with a second document.
    if await repo.permit_by_application(db, application_id) is not None:
        raise err("ERR-PERM-001", details={"reason": "already_issued"})

    # 3. Everything the document says, gathered once. A DRAFT is autosaved field
    # by field, so most of these columns are nullable (3.9a ruling 7) and each is
    # resolved through `_required` HERE — a missing requisite is named at its
    # source rather than surfacing as a blank line on a rendered permit.
    contour_id = _required(application.contour_id, field="contour_id")
    activity_type_id = _required(application.activity_type_id, field="activity_type_id")
    contour_version_id = _required(application.contour_version_id, field="contour_version_id")
    area_ha = _required(application.requested_area_ha, field="area_ha")
    period_from = _required(application.period_from, field="period_from")
    period_to = _required(application.period_to, field="period_to")

    organization_id = await _assert_in_zone(db, actor, contour_id)

    calculation = await applications_service.current_calculation(db, application_id)
    if calculation is None:
        raise err("ERR-VAL-001", details={"reason": "no_calculation"})
    template = await repo.active_template(db, activity_type_id)
    if template is None:
        raise err(
            "ERR-VAL-001",
            details={
                "reason": "no_active_template",
                "activity_type_id": str(activity_type_id),
            },
        )
    layout_html = await _layout_html(db, template)

    # 4. The number. One UPDATE ... RETURNING under the row lock (ruling 9).
    series = get_settings().permit_series
    number = await repo.next_number(db, series)
    if number is None:
        # Configuration naming a series the database has no counter row for —
        # classically a Latin `A` where the seeded key is Cyrillic `А`. Refuse:
        # carrying on would write a permit with no number at all.
        raise err("ERR-SYS-001", details={"reason": "unknown_permit_series", "series": series})

    snapshot = await _snapshot(
        db,
        series=series,
        number=number,
        applicant_id=application.applicant_id,
        activity_type_id=activity_type_id,
        organization_id=organization_id,
        contour_id=contour_id,
        area_ha=area_ha,
        period_from=period_from,
        period_to=period_to,
        amount=calculation.amount,
        sb_load=calculation.used_sb,
        calculation_id=calculation.id,
    )

    # 5. The QR token is a SECRET, not an identifier (ruling 8): never derived
    # from the series, the number or the id, and never returned in a response.
    qr_token = secrets.token_urlsafe(32)

    # 6. Rendering is blocking C code (Pango, HarfBuzz) — off the event loop, the
    # same rule 3.6a applies to `pyogrio`.
    pdf = await asyncio.to_thread(render.render_permit, snapshot, layout_html, qr_url(qr_token))

    # 7. The bytes, then their hash. `save_upload` writes to MinIO BEFORE the DB
    # flush on purpose: a storage failure aborts the transaction and leaves no
    # dangling row, while a dangling object is harmless garbage.
    document = await files.save_upload(
        db,
        data=pdf,
        filename=f"permit-{series}-{number:06d}.pdf",
        content_type="application/pdf",
        actor=actor,
    )

    permit = Permit(
        series=series,
        number=number,
        application_id=application.id,
        applicant_id=application.applicant_id,
        activity_type_id=activity_type_id,
        organization_id=organization_id,
        contour_id=contour_id,
        contour_version_id=contour_version_id,
        area_ha=area_ha,
        period_from=period_from,
        period_to=period_to,
        amount=calculation.amount,
        sb_load=calculation.used_sb,
        status=INITIAL_STATUS,
        pdf_file_id=document.id,
        doc_hash=hashlib.sha256(pdf).hexdigest(),
        qr_token=qr_token,
        template_id=template.id,
        snapshot=snapshot,
    )
    # 8. The row and its timeline.
    await repo.add(db, permit)
    await repo.add_status_history(
        db,
        PermitStatusHistory(
            permit_id=permit.id,
            from_status=None,
            to_status=INITIAL_STATUS,
            changed_by=actor.id,
        ),
    )
    # A plain INSERT's implicit RETURNING covers only what the DB generates, so a
    # caller-supplied value in a fixed-scale NUMERIC still reads back at the
    # posted scale until refreshed (lesson: the row in memory is not what Postgres
    # stored) — and this row is serialized straight into the response.
    await db.refresh(permit)

    # 9. NOT `set_status(..., PERMIT_ISSUED)`. See the docstring: ruling 18.

    # 10. The trail, in the same transaction as the action (the audit invariant).
    await audit.log(
        db,
        action=PERMIT_ISSUE,
        user_id=actor.id,
        object_type="permit",
        object_id=permit.id,
        new_value={
            "series": series,
            "number": number,
            "application_id": str(application.id),
            "doc_hash": permit.doc_hash,
            "template_id": str(template.id),
        },
    )

    # 11. The applicant is told. `owner_user_id` is the individual's own account;
    # a legal entity has none (decision #9), so the submitter — a representative
    # acting for it — is who hears about it.
    await notifications.notify(
        db,
        event_code=events.PERMIT_ISSUED,
        recipient_user_id=await _notification_recipient(
            db,
            applicant_id=application.applicant_id,
            submitted_by_user_id=application.submitted_by_user_id,
        ),
        params={
            "permit_number": f"{series} № {number:06d}",
            "valid_from": period_from,
            "valid_to": period_to,
        },
        object_type="permit",
        object_id=permit.id,
    )
    return permit


async def _notification_recipient(
    db: AsyncSession, *, applicant_id: uuid.UUID, submitted_by_user_id: uuid.UUID
) -> uuid.UUID:
    """Who hears about this application's permit: the individual applicant's own
    account when there is one, otherwise whoever filed it. A legal entity has no
    `owner_user_id` (decision #9) and is reached through the representative who
    acted for it, which is also the fallback for an applicant row that has somehow
    lost its account — `notify` raises on a recipient it cannot resolve, and a
    permit must not fail to issue over a notification."""
    applicant = await auth_service.get_applicant(db, applicant_id)
    if applicant is not None and applicant.owner_user_id is not None:
        return applicant.owner_user_id
    return submitted_by_user_id


# --- the in-process read surface --------------------------------------------
#
# Task 8 owns this module's full public surface (`get`, `set_status`) and the
# comment block describing it. The two below land here because issuance's own
# tests are their first caller, and both follow the rule every sibling read
# follows (`gis.service.published_version`, `applications.service.get`): no
# permission and no zone rule, because the caller is another SERVICE inside this
# process — the gates live on the routes that reach them.


async def for_application(db: AsyncSession, application_id: uuid.UUID) -> Permit | None:
    """The permit issued for this application, or None."""
    return await repo.permit_by_application(db, application_id)


async def pdf_bytes(db: AsyncSession, permit_id: uuid.UUID) -> bytes:
    """The stored document — the exact bytes `doc_hash` was taken over and every
    ERI signature covers.

    Reads storage directly rather than through `core.files.get_readable`, which
    applies the FILE subsystem's own rule: the uploader, or any non-applicant
    role. The uploader here is the issuing hodim, so `get_readable` would refuse
    the permit's own HOLDER their own permit — the opposite of the access rule
    this document needs. The download ROUTE (Task 8) applies the permit's rule;
    this function, like every read above it, applies none.
    """
    permit = await repo.permit_by_id(db, permit_id)
    if permit is None or permit.pdf_file_id is None:
        raise err("ERR-SYS-003", details={"permit": str(permit_id)})
    file = await db.get(MediaFile, permit.pdf_file_id)
    if file is None:
        raise err("ERR-SYS-003", details={"permit": str(permit_id)})
    return await storage.get_object(file.storage_key)
