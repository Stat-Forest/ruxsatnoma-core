"""`archive`'s business logic: archive one eligible application or permit on
request (plan ruling 4 — no nightly sweep in this cut), store a hashed
snapshot in object storage (ruling 5), and read the register back zone-scoped.

**Eligibility has exactly one source per kind**: the owning module's own
transition table. `_ELIGIBLE_APPLICATION_STATUSES`/`_ELIGIBLE_PERMIT_STATUSES`
below are DERIVED from `applications.service.APPLICATION_TRANSITIONS` /
`permits.service.PERMIT_TRANSITIONS` — the sets of source statuses each table
already allows into `ARCHIVED`/`archived` — never retyped, so a future change
to either transition table (e.g. the Agency's answer on `tz/12` #16 opening a
new edge into `archived`) widens what this module accepts with no edit here.

The actual status move goes through that module's own `set_status` — the ONE
way any module outside it may do so — never a direct `UPDATE`, and never an
import of `applications.repo`/`.models`/`permits.repo`/`.models`."""

import hashlib
import json
import uuid
from datetime import date
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.abac import Zone, zone_filter, zone_of
from app.core.errors import err
from app.core.schemas import Page, PageParams
from app.modules.admin import service as admin_service
from app.modules.admin.models import Organization
from app.modules.applications import service as applications_service
from app.modules.applications.service import APPLICATION_TRANSITIONS
from app.modules.archive import repo
from app.modules.archive.models import ArchiveItem
from app.modules.archive.schemas import ArchiveItemOut
from app.modules.audit import service as audit
from app.modules.auth.models import User
from app.modules.permits import service as permits_service
from app.modules.permits.service import PERMIT_TRANSITIONS

ITEM_CREATE = "archive.item_create"
ITEM_VERIFY = "archive.item_verify"

_ELIGIBLE_APPLICATION_STATUSES = frozenset(
    status for status, targets in APPLICATION_TRANSITIONS.items() if "ARCHIVED" in targets
)
_ELIGIBLE_PERMIT_STATUSES = frozenset(
    status for status, targets in PERMIT_TRANSITIONS.items() if "archived" in targets
)


def _json_default(value: Any) -> str:
    """`Decimal`/`date`/`datetime`/`UUID` all serialize as `str` — nothing in
    this app configures a JSON encoder (lesson), so every boundary coerces by
    hand; this is the snapshot's own boundary."""
    return str(value)


def _snapshot_application(application: Any) -> dict[str, Any]:
    return {
        "id": application.id,
        "number": application.number,
        "status": application.status,
        "applicant_id": application.applicant_id,
        "activity_type_id": application.activity_type_id,
        "contour_id": application.contour_id,
        "assigned_org_id": application.assigned_org_id,
        "period_from": application.period_from,
        "period_to": application.period_to,
        "submitted_at": application.submitted_at,
        "decided_at": application.decided_at,
    }


def _snapshot_permit(permit: Any) -> dict[str, Any]:
    return {
        "id": permit.id,
        "series": permit.series,
        "number": permit.number,
        "status": permit.status,
        "applicant_id": permit.applicant_id,
        "organization_id": permit.organization_id,
        "contour_id": permit.contour_id,
        "amount": permit.amount,
        "period_from": permit.period_from,
        "period_to": permit.period_to,
        "issued_at": permit.issued_at,
        "doc_hash": permit.doc_hash,
        "pdf_file_id": permit.pdf_file_id,
    }


def _organization_in_zone(zone: Zone, org: Organization) -> bool:
    """Per-row equivalent of `zone_filter`'s SQL for ONE organization row — a
    LOCAL copy of the private helper of the same name and identical logic in
    `gis.service`, `norms.service` and `permits.service`. The module boundary
    rules out importing any of them: it is not part of their declared public
    surface."""
    if zone.region_id is not None and zone.region_id != org.region_id:
        return False
    if zone.district_id is not None and zone.district_id != org.district_id:
        return False
    if zone.organization_id is not None and zone.organization_id != org.id:
        return False
    return True


async def _assert_in_zone(db: AsyncSession, actor: User, organization_id: uuid.UUID | None) -> None:
    zone = zone_of(actor)
    if zone.region_id is None and zone.district_id is None and zone.organization_id is None:
        return  # republic-wide actor (central_admin without a narrower zone)
    if organization_id is None:
        # A zone-scoped actor cannot prove an org-less object is theirs —
        # fail closed, the same posture `zone_filter` itself takes.
        raise err("ERR-ACL-002")
    org = await admin_service.organization_or_404(db, organization_id)
    if not _organization_in_zone(zone, org):
        raise err("ERR-ACL-002")


async def archive_object(
    db: AsyncSession,
    *,
    actor: User,
    object_type: str,
    object_id: uuid.UUID,
    retention_until: date | None,
) -> ArchiveItem:
    # Zone BEFORE status, on both branches — the same order `permits.service`
    # settled on after its own review found `issue_duplicate` checking status
    # first and telling an out-of-zone caller more than "not yours" (decision
    # #69): existence (404) is unavoidably first, since the organization to
    # check against comes off the row itself, but everything after that is
    # zone, then eligibility, never the reverse.
    if object_type == "application":
        application = await applications_service.get(db, object_id)
        if application is None:
            raise err("ERR-SYS-003")
        organization_id = application.assigned_org_id
        await _assert_in_zone(db, actor, organization_id)
        if application.status not in _ELIGIBLE_APPLICATION_STATUSES:
            raise err(
                "ERR-ARCH-001",
                details={"reason": "not_archivable_status", "status": application.status},
            )
        snapshot = _snapshot_application(application)
        target_status = "ARCHIVED"
    elif object_type == "permit":
        permit = await permits_service.get(db, object_id)
        if permit is None:
            raise err("ERR-SYS-003")
        organization_id = permit.organization_id
        await _assert_in_zone(db, actor, organization_id)
        if permit.status not in _ELIGIBLE_PERMIT_STATUSES:
            raise err(
                "ERR-ARCH-001",
                details={"reason": "not_archivable_status", "status": permit.status},
            )
        snapshot = _snapshot_permit(permit)
        target_status = "archived"
    else:  # pragma: no cover — the router's Literal already refuses this
        raise err("ERR-VAL-001", details={"reason": "unknown_object_type"})

    body = json.dumps(snapshot, sort_keys=True, default=_json_default).encode()
    content_hash = hashlib.sha256(body).hexdigest()
    storage_ref = f"archive/{object_type}/{object_id}.json"
    await storage.put_object(storage_ref, body, content_type="application/json")

    if object_type == "application":
        await applications_service.set_status(db, object_id, to_status=target_status, actor=actor)
    else:
        await permits_service.set_status(db, object_id, to_status=target_status, actor=actor)

    item = ArchiveItem(
        object_type=object_type,
        object_id=object_id,
        organization_id=organization_id,
        retention_until=retention_until,
        content_hash=content_hash,
        storage_ref=storage_ref,
        status="stored",
        created_by=actor.id,
    )
    try:
        async with db.begin_nested():
            await repo.add(db, item)
    except IntegrityError as exc:
        cause = exc.orig.__cause__ if exc.orig is not None else None
        if getattr(cause, "constraint_name", None) != "uq_archive_items_object":
            raise
        raise err("ERR-ARCH-001", details={"reason": "already_archived"}) from exc

    await audit.log(
        db,
        action=ITEM_CREATE,
        user_id=actor.id,
        object_type=object_type,
        object_id=object_id,
        new_value={"archive_item_id": str(item.id), "storage_ref": storage_ref},
    )
    return item


async def list_items(
    db: AsyncSession,
    *,
    actor: User,
    params: PageParams,
    object_type: str | None,
    status: str | None,
) -> Page[ArchiveItemOut]:
    scope = zone_filter(
        zone_of(actor),
        region_col=Organization.region_id,
        district_col=Organization.district_id,
        organization_col=ArchiveItem.organization_id,
    )
    rows, total = await repo.list_items(
        db,
        scope=scope,
        object_type=object_type,
        status=status,
        offset=params.offset,
        limit=params.page_size,
    )
    items = [ArchiveItemOut.model_validate(row) for row in rows]
    return Page(items=items, total=total, page=params.page, page_size=params.page_size)


async def get_item(db: AsyncSession, actor: User, item_id: uuid.UUID) -> ArchiveItem:
    item = await repo.item_by_id(db, item_id)
    if item is None:
        raise err("ERR-SYS-003")
    await _assert_in_zone(db, actor, item.organization_id)
    return item


async def verify_item(db: AsyncSession, actor: User, item_id: uuid.UUID) -> ArchiveItem:
    """Re-fetch the stored snapshot, re-hash it, and compare — this checks
    STORAGE integrity (bit-rot/tampering since it was written), not database
    drift: the archived object's snapshot is a frozen fact, not a live view.
    A mismatch raises `ERR-ARCH-002` and leaves `status` untouched; only a
    PASSING check ever writes `verified` (plan ruling 5 — the failure itself
    is the alarm)."""
    item = await repo.item_by_id_for_update(db, item_id)
    if item is None:
        raise err("ERR-SYS-003")
    await _assert_in_zone(db, actor, item.organization_id)
    body = await storage.get_object(item.storage_ref)
    actual_hash = hashlib.sha256(body).hexdigest()
    if actual_hash != item.content_hash:
        raise err(
            "ERR-ARCH-002",
            details={"reason": "hash_mismatch", "expected": item.content_hash},
        )
    item.status = "verified"
    await db.flush()
    await audit.log(
        db,
        action=ITEM_VERIFY,
        user_id=actor.id,
        object_type=item.object_type,
        object_id=item.object_id,
    )
    return item
