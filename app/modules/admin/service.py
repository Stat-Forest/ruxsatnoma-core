"""admin service: the only door into reference data for other modules.

Read helpers are thin (the rules live in repo queries); the write helpers in Tasks 5
and 6 carry the hierarchy rules, archival semantics and the audit trail.
"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.modules.admin import repo
from app.modules.admin.models import Classifier, ClassifierItem, Organization
from app.modules.admin.schemas import OrganizationIn, OrganizationPatch
from app.modules.audit import service as audit
from app.modules.auth.models import User


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


# Which parent kind each kind may hang off (ruling 6). `leshoz` accepts both because
# republic-subordinated leshozes report to the agency directly (old system's
# Department.management flag).
ALLOWED_PARENT_KINDS: dict[str, tuple[str, ...]] = {
    "agency": (),
    "territorial": ("agency",),
    "leshoz": ("agency", "territorial"),
    "bolim": ("leshoz",),
    "aylanma": ("bolim",),
    "bolak": ("aylanma",),
}

_AUDITED_FIELDS = (
    "parent_id",
    "kind",
    "code",
    "name",
    "stir",
    "region_id",
    "district_id",
    "requisites",
    "status",
)


def _snapshot(org: Organization) -> dict[str, Any]:
    """JSON-safe view of an organization for audit old_value/new_value."""
    data: dict[str, Any] = {}
    for field in _AUDITED_FIELDS:
        value = getattr(org, field)
        data[field] = str(value) if isinstance(value, uuid.UUID) else value
    return data


async def _validate_parent(db: AsyncSession, *, kind: str, parent_id: uuid.UUID | None) -> None:
    allowed = ALLOWED_PARENT_KINDS.get(kind)
    if allowed is None:
        raise err("ERR-VAL-001", details={"kind": kind, "reason": "unknown kind"})
    if not allowed:  # agency: root only
        if parent_id is not None:
            raise err("ERR-VAL-001", details={"kind": kind, "reason": "must be root"})
        return
    if parent_id is None:
        raise err("ERR-VAL-001", details={"kind": kind, "reason": "parent required"})
    parent = await repo.get_organization(db, parent_id)
    if parent is None:
        raise err("ERR-SYS-003", details={"organization": str(parent_id)})
    if parent.kind not in allowed:
        raise err(
            "ERR-VAL-001",
            details={"kind": kind, "parent_kind": parent.kind, "allowed": list(allowed)},
        )
    if parent.status != "active":
        raise err("ERR-VAL-001", details={"reason": "parent archived"})


async def create_organization(
    db: AsyncSession, *, data: OrganizationIn, actor: User
) -> Organization:
    if await repo.get_organization_by_code(db, data.code) is not None:
        raise err("ERR-VAL-001", details={"code": data.code, "reason": "already exists"})
    if data.kind == "agency" and await repo.get_agency(db) is not None:
        # The partial unique index would raise IntegrityError → 500; a domain error
        # tells the admin what is actually wrong (ruling 6).
        raise err("ERR-VAL-001", details={"kind": "agency", "reason": "root already exists"})
    await _validate_parent(db, kind=data.kind, parent_id=data.parent_id)
    org = Organization(
        parent_id=data.parent_id,
        kind=data.kind,
        code=data.code,
        name=data.name.root,
        stir=data.stir,
        region_id=data.region_id,
        district_id=data.district_id,
        requisites=data.requisites,
    )
    await repo.add(db, org)
    await audit.log(
        db,
        action="organization.create",
        user_id=actor.id,
        object_type="organization",
        object_id=org.id,
        new_value=_snapshot(org),
    )
    return org


async def update_organization(
    db: AsyncSession, *, org_id: uuid.UUID, patch: OrganizationPatch, actor: User
) -> Organization:
    org = await organization_or_404(db, org_id)
    before = _snapshot(org)
    fields = patch.model_dump(exclude_unset=True)

    if "parent_id" in fields:
        new_parent = fields["parent_id"]
        if new_parent == org.id:
            raise err("ERR-VAL-001", details={"reason": "cycle"})
        if new_parent is not None and await repo.is_descendant(
            db, ancestor_id=org.id, candidate_id=new_parent
        ):
            raise err("ERR-VAL-001", details={"reason": "cycle"})
        await _validate_parent(db, kind=org.kind, parent_id=new_parent)
        org.parent_id = new_parent
    if "name" in fields and patch.name is not None:
        org.name = patch.name.root
    for field in ("stir", "region_id", "district_id", "requisites"):
        if field in fields:
            setattr(org, field, fields[field])
    await db.flush()
    await audit.log(
        db,
        action="organization.update",
        user_id=actor.id,
        object_type="organization",
        object_id=org.id,
        old_value=before,
        new_value=_snapshot(org),
    )
    return org


async def archive_organization(db: AsyncSession, *, org_id: uuid.UUID, actor: User) -> Organization:
    """Archival replaces deletion (design/02 principle 7). A node with active children
    keeps them reachable, so the subtree is archived leaves-first by the admin."""
    org = await organization_or_404(db, org_id)
    if org.status == "archived":
        return org
    active_children = (
        await db.execute(
            select(func.count())
            .select_from(Organization)
            .where(Organization.parent_id == org.id, Organization.status == "active")
        )
    ).scalar_one()
    if active_children:
        raise err("ERR-VAL-001", details={"reason": "active children", "count": active_children})
    before = _snapshot(org)
    org.status = "archived"
    await db.flush()
    await audit.log(
        db,
        action="organization.archive",
        user_id=actor.id,
        object_type="organization",
        object_id=org.id,
        old_value=before,
        new_value=_snapshot(org),
    )
    return org
