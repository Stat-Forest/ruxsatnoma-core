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
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.numbers import next_public_number
from app.core.schemas import LOCALES
from app.core.time import TASHKENT, business_today
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import ClassifierItem
from app.modules.applications import printout_labels as labels
from app.modules.applications import repo
from app.modules.applications.models import (
    PRINTOUT_REJECTION_NOTICE,
    Application,
    ApplicationPrintout,
    ApplicationRejectionGround,
)
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.signatures import service as signatures_service
from app.modules.signatures.models import Signature

FALLBACK_LANGUAGE = "uz_latn"
NOTICE_NUMBER_PREFIX = "RD"


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
