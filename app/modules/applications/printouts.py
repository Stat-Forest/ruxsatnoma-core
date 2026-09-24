"""Stage 16 — the application's two printed documents (rulings R5-R7, R11-R12).

A printout is a frozen SNAPSHOT (strings only, in one language) written in the
transaction of the event it records — a filing, a rejection — and a PDF
rendered from it on the first download (`service.printout_pdf`). Nothing here
re-reads a source after the snapshot is written: what the snapshot says is
what the document says.

Imports no `service`/`decision` — both import this module. Whatever needs
`service`'s private rules (the recipient, the leshoz) arrives as a parameter.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import pdf
from app.core.numbers import next_public_number
from app.core.schemas import LOCALES
from app.core.time import TASHKENT, business_today
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import ClassifierItem
from app.modules.applications import printout_labels as labels
from app.modules.applications import repo
from app.modules.applications.models import (
    PRINTOUT_LETTER,
    PRINTOUT_REJECTION_NOTICE,
    Application,
    ApplicationDocument,
    ApplicationItem,
    ApplicationPrintout,
    ApplicationRejectionGround,
)
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.signatures import service as signatures_service
from app.modules.signatures.models import Signature

FALLBACK_LANGUAGE = "uz_latn"
NOTICE_NUMBER_PREFIX = "RD"

LAYOUTS_DIR = Path(__file__).parent / "assets" / "printouts"


@lru_cache(maxsize=3)
def _layout(name: str) -> str:
    return (LAYOUTS_DIR / f"{name}.html").read_text(encoding="utf-8")


def render_pdf(kind: str, language: str, snapshot: dict[str, Any]) -> bytes:
    """One printout to PDF/A bytes, from its frozen snapshot alone. Blocking —
    call through `asyncio.to_thread`. `created` pins the metadata, so a
    re-render of the same snapshot is byte-identical (ruling R6)."""
    created = snapshot.get("created")
    if kind == PRINTOUT_LETTER:
        values = {**snapshot, **labels.LETTER_LABELS[language]}
        values[pdf.QR_FIELD] = pdf.qr_png_data_uri(
            f"{snapshot['number']} sha256:{snapshot['package_sha256']}"
        )
        return pdf.render_document(_layout("letter"), values, created=created)
    notice_labels = labels.NOTICE_LABELS[language]
    ground_layout = _layout("notice_ground")
    grounds_html = "".join(
        pdf.fill(ground_layout, {**g, **notice_labels}) for g in snapshot["grounds"]
    )
    values = {k: v for k, v in snapshot.items() if k != "grounds"} | notice_labels
    return pdf.render_document(
        _layout("notice"), values, fragments={"grounds": grounds_html}, created=created
    )


def _name(value: dict[str, Any] | None, language: str) -> str:
    """A localized DB name in `language`, `uz_latn` when it has none (#90)."""
    if not value:
        return labels.NOT_STATED
    return value.get(language) or value.get(FALLBACK_LANGUAGE) or labels.NOT_STATED


def _date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value is not None else labels.NOT_STATED


def _datetime(value: datetime | None) -> str:
    if value is None:
        return labels.NOT_STATED
    return value.astimezone(TASHKENT).strftime("%d.%m.%Y %H:%M")


def _decimal(value: Decimal | None) -> str:
    if value is None:
        return labels.NOT_STATED
    return f"{value.normalize():f}"


def _iso(value: datetime) -> str:
    """The PDF's own clock (`pdf.render_document(created=…)`): WeasyPrint turns a
    W3C/ISO string into `/CreationDate`, so this is ISO, never the printed
    `dd.mm.yyyy` form."""
    return value.astimezone(TASHKENT).replace(microsecond=0).isoformat()


async def recipient_language(db: AsyncSession, user_id: uuid.UUID) -> str:
    """The document language (ruling R5): the recipient's account language."""
    contact = await auth_service.get_notification_contact(db, user_id)
    language = contact.language if contact is not None else FALLBACK_LANGUAGE
    return language if language in LOCALES else FALLBACK_LANGUAGE


def _signature_line(signature: Signature, language: str, serial: str | None) -> str:
    if signature.kind == "simple" or serial is None:
        return labels.SIGNATURE_SIMPLE_FORMAT[language].format(ref=str(signature.id))
    return labels.SIGNATURE_ERI_FORMAT[language].format(serial=serial)


async def _certificate_serial(db: AsyncSession, signature: Signature) -> str | None:
    if signature.certificate_id is None:
        return None
    certificate = await signatures_service.get_certificate(db, signature.certificate_id)
    return certificate.serial_number


async def _applicant_address(db: AsyncSession, applicant: Any) -> str:
    """Region, district, street — the permit's own composition
    (`permits.service._holder_address`), `—` when the registry holds nothing."""
    parts: list[str] = []
    if applicant.region_id is not None:
        region = await admin_repo.get_region(db, applicant.region_id)
        if region is not None:
            parts.append(_name(region.name, FALLBACK_LANGUAGE))
    if applicant.district_id is not None:
        district = await admin_repo.get_district(db, applicant.district_id)
        if district is not None:
            parts.append(_name(district.name, FALLBACK_LANGUAGE))
    if applicant.address:
        parts.append(applicant.address)
    filled = [p for p in parts if p and p != labels.NOT_STATED]
    return ", ".join(filled) if filled else labels.NOT_STATED


async def record_rejection_notice(
    db: AsyncSession,
    application: Application,
    *,
    grounds: list[ApplicationRejectionGround],
    reason_items: list[ClassifierItem],
    reapply_text: str,
    appeal_text: str,
    signature: Signature,
    signer: User,
    recipient_user_id: uuid.UUID,
    organization_id: uuid.UUID | None,
) -> ApplicationPrintout:
    """Freeze the rejection notice (ruling R6) — called by `decision.reject`
    after the grounds are stored, inside the same transaction; the number is
    allocated here, so a rejection that rolls back leaves no hole (R12)."""
    language = await recipient_language(db, recipient_user_id)
    applicant = await auth_service.get_applicant(db, application.applicant_id)
    organization = (
        await admin_repo.get_organization(db, organization_id) if organization_id else None
    )
    role = await auth_service.role_of(db, signer)
    number = await next_public_number(db, NOTICE_NUMBER_PREFIX, business_today())
    decided_at = application.decided_at
    email = applicant.email if applicant is not None else None
    snapshot = {
        "number": number,
        "decided_at": _date(decided_at.astimezone(TASHKENT).date() if decided_at else None),
        "application_number": application.number or labels.NOT_STATED,
        "addressee": labels.NOTICE_ADDRESSEE_FORMAT[language].format(
            name=applicant.name if applicant is not None else labels.NOT_STATED
        ),
        "addressee_address": (
            await _applicant_address(db, applicant) if applicant is not None else labels.NOT_STATED
        ),
        "addressee_contact": email or labels.PERSONAL_CABINET[language],
        "reviewer": (
            f"{_name(organization.name if organization else None, language)} / {signer.full_name}"
        ),
        "body": labels.NOTICE_BODY_FORMAT[language].format(
            date=_date(
                application.submitted_at.astimezone(TASHKENT).date()
                if application.submitted_at
                else None
            ),
            number=application.number or labels.NOT_STATED,
        ),
        "grounds": [
            {
                "index": str(ground.position),
                "code": item.code,
                "name": _name(item.name, language),
                "fact": ground.fact,
                "legal": f"{ground.legal_document}, {ground.legal_clause}",
                "evidence": ground.evidence,
                "remedy": ground.remedy,
            }
            for ground, item in zip(grounds, reason_items, strict=True)
        ],
        "reapply_text": reapply_text,
        "appeal_text": appeal_text,
        "moderator": f"{_name(role.name if role else None, language)}, {signer.full_name}",
        "signature": _signature_line(signature, language, await _certificate_serial(db, signature)),
        "signed_at": _datetime(signature.signed_at),
        "created": _iso(signature.signed_at),
    }
    row = ApplicationPrintout(
        application_id=application.id,
        kind=PRINTOUT_REJECTION_NOTICE,
        number=number,
        language=language,
        snapshot=snapshot,
    )
    await repo.add_printout(db, row)
    return row


# --- Task B4 (rulings R6/R7/R11): the application letter's frozen snapshot ---


async def _addressee(db: AsyncSession, organization_id: uuid.UUID | None, language: str) -> str:
    """«{leshoz} rahbari {name}ga». The name is printed only when exactly one
    active user of the leshoz holds `applications.decide` — the head; with
    none or several, the line names the office, never a guess."""
    organization = (
        await admin_repo.get_organization(db, organization_id) if organization_id else None
    )
    org = _name(organization.name if organization else None, language)
    heads = (
        await auth_service.user_ids_with_permission(
            db, "applications.decide", organization_id=organization_id
        )
        if organization_id
        else []
    )
    if len(heads) == 1:
        head = await db.get(User, heads[0])
        if head is not None:
            return labels.LETTER_ADDRESSEE_FORMAT[language].format(org=org, name=head.full_name)
    return labels.LETTER_ADDRESSEE_NO_NAME_FORMAT[language].format(org=org)


async def _quantity_line(
    db: AsyncSession,
    application: Application,
    activity: Any,
    items: list[ApplicationItem],
    language: str,
) -> str:
    if items:
        names = {t.id: t.name for t in await admin_repo.list_livestock_types(db)}
        return "; ".join(
            f"{_name(names.get(i.livestock_type_id), language)}: {i.head_count}" for i in items
        )
    if application.quantity is None or activity is None:
        return labels.NOT_STATED
    unit = labels.UNIT_LABELS[language].get(activity.quantity_unit, activity.quantity_unit)
    return f"{_decimal(application.quantity)} {unit}"


def _purpose_line(application: Application, activity_name: str, language: str) -> str:
    if application.deadwood_product:
        product = labels.DEADWOOD_PRODUCT_LABELS[language].get(
            application.deadwood_product, application.deadwood_product
        )
        return labels.PURPOSE_DEADWOOD_FORMAT[language].format(
            product=product, deadline=_date(application.removal_deadline)
        )
    if application.recreation_purpose:
        purpose = labels.RECREATION_PURPOSE_LABELS[language].get(
            application.recreation_purpose, application.recreation_purpose
        )
        return labels.PURPOSE_RECREATION_FORMAT[language].format(
            purpose=purpose, event_at=_datetime(application.event_at)
        )
    return activity_name


async def record_letter(
    db: AsyncSession,
    application: Application,
    *,
    submission_id: uuid.UUID,
    signature: Signature,
    items: list[ApplicationItem],
    documents: list[ApplicationDocument],
    recipient_user_id: uuid.UUID,
) -> ApplicationPrintout:
    """Freeze the letter of one submission (rulings R6/R7) — called by
    `service.file` and `service.submit` at their very end, in their transaction."""
    language = await recipient_language(db, recipient_user_id)
    applicant = await auth_service.get_applicant(db, application.applicant_id)
    activity = (
        await admin_repo.get_activity_type(db, application.activity_type_id)
        if application.activity_type_id
        else None
    )
    activity_name = _name(activity.name if activity else None, language)
    organization_id = (
        await gis_service.contour_organization(db, application.contour_id)
        if application.contour_id
        else None
    )
    organization = (
        await admin_repo.get_organization(db, organization_id) if organization_id else None
    )
    region = (
        await admin_repo.get_region(db, organization.region_id)
        if organization is not None and organization.region_id
        else None
    )
    district = (
        await admin_repo.get_district(db, organization.district_id)
        if organization is not None and organization.district_id
        else None
    )
    contour = (
        await gis_service.contour_number(db, application.contour_id)
        if application.contour_id
        else None
    )
    point = (
        await gis_service.version_point(db, application.contour_version_id)
        if application.contour_version_id
        else None
    )
    doc_names: list[str] = []
    for document in documents:
        item = await admin_repo.get_classifier_item(db, document.doc_type_item_id)
        doc_names.append(_name(item.name if item else None, language))
    submitter = await db.get(User, application.submitted_by_user_id)
    name = applicant.name if applicant is not None else labels.NOT_STATED
    if applicant is not None and applicant.kind == "legal" and submitter is not None:
        name = f"{applicant.name} ({submitter.full_name})"
    contact = (
        " • ".join(
            x
            for x in (
                (applicant.phone if applicant else None)
                or (submitter.phone if submitter else None),
                (applicant.email if applicant else None)
                or (submitter.email if submitter else None),
            )
            if x
        )
        or labels.NOT_STATED
    )
    snapshot = {
        "number": application.number or labels.NOT_STATED,
        "date": _date(
            application.submitted_at.astimezone(TASHKENT).date()
            if application.submitted_at
            else None
        ),
        "addressee": await _addressee(db, organization_id, language),
        "applicant_name": name,
        "applicant_address": await _applicant_address(db, applicant)
        if applicant
        else labels.NOT_STATED,
        "contact": contact,
        "activity_name": activity_name,
        "territory": " / ".join(
            (
                _name(region.name if region else None, language),
                _name(district.name if district else None, language),
                _name(organization.name if organization else None, language),
            )
        ),
        "plot": labels.PLOT_FORMAT[language].format(
            contour=contour or labels.NOT_STATED, area=_decimal(application.requested_area_ha)
        ),
        "coordinates": f"{point[0]:.6f}, {point[1]:.6f}" if point else labels.NOT_STATED,
        "period": labels.PERIOD_FORMAT[language].format(
            start=_date(application.period_from), end=_date(application.period_to)
        ),
        "quantity": await _quantity_line(db, application, activity, items, language),
        "purpose": _purpose_line(application, activity_name, language),
        "attachments": "; ".join(doc_names) or labels.NOT_STATED,
        "signed_at": _datetime(signature.signed_at),
        "signature": _signature_line(signature, language, await _certificate_serial(db, signature)),
        "package_sha256": signature.doc_hash,
        "created": _iso(signature.signed_at),
    }
    row = ApplicationPrintout(
        application_id=application.id,
        kind=PRINTOUT_LETTER,
        submission_id=submission_id,
        language=language,
        snapshot=snapshot,
    )
    await repo.add_printout(db, row)
    return row
