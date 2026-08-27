"""admin service: the only door into reference data for other modules.

Read helpers are thin (the rules live in repo queries); the write helpers in Tasks 5
and 6 carry the hierarchy rules, archival semantics and the audit trail.
"""

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.modules.admin import repo
from app.modules.admin.models import Classifier, ClassifierItem, Organization


async def classifier_items_by_code(
    db: AsyncSession, code: str, *, on_date: date | None = None
) -> list[ClassifierItem]:
    """Items of a classifier addressed by its code; 404 when the classifier is unknown."""
    classifier = await _classifier_or_404(db, code)
    return await repo.list_classifier_items(db, classifier.id, on_date=on_date)


async def _classifier_or_404(db: AsyncSession, code: str) -> Classifier:
    classifier = await repo.get_classifier_by_code(db, code)
    if classifier is None:
        raise err("ERR-SYS-003", details={"classifier": code})
    return classifier


async def organization_or_404(db: AsyncSession, org_id: uuid.UUID) -> Organization:
    org = await repo.get_organization(db, org_id)
    if org is None:
        raise err("ERR-SYS-003", details={"organization": str(org_id)})
    return org
