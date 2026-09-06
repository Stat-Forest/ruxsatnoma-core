"""admin service: the only door into reference data for other modules.

Read helpers are thin (the rules live in repo queries); the write helpers in Tasks 5
and 6 carry the hierarchy rules, archival semantics and the audit trail.
"""

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.errors import err
from app.core.models import SystemSetting
from app.core.time import business_today
from app.modules.admin import repo
from app.modules.admin.models import Classifier, ClassifierItem, Organization, Region
from app.modules.admin.schemas import (
    ClassifierIn,
    ClassifierItemIn,
    ClassifierItemPatch,
    OrganizationIn,
    OrganizationPatch,
    SettingOut,
)
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


async def list_regions(db: AsyncSession) -> list[Region]:
    """Thin pass-through (4.6 `public`'s open-data stats need region names to
    group by, and cross-module reads go through THIS service, never
    `admin.repo` directly — design/01 rule 2). No rule to apply: identical
    reasoning to why `GET /refs/*` calls the repo straight from its own
    router, just on the other side of the module boundary."""
    return await repo.list_regions(db)


# The whole country tops out at roughly ninety leshozes (plan `03.6a` note); a
# caller needing "every organization of a kind" is asking a reference-data
# question, not a paged-listing one, so this is not `repo.list_organizations`'
# own `limit=20` default.
_ALL_ORGANIZATIONS_LIMIT = 1000


async def list_organizations(
    db: AsyncSession, *, kind: str | None = None, status: str | None = "active"
) -> list[Organization]:
    """Thin pass-through, unpaged (see `_ALL_ORGANIZATIONS_LIMIT`) — the open-data
    aggregate (4.6 `public`) needs every organization of a kind to group permit
    counts by region, not one page of them."""
    rows, _total = await repo.list_organizations(
        db, kind=kind, status=status, limit=_ALL_ORGANIZATIONS_LIMIT
    )
    return rows


async def organization_or_404(db: AsyncSession, org_id: uuid.UUID) -> Organization:
    org = await repo.get_organization(db, org_id)
    if org is None:
        raise err("ERR-SYS-003", details={"organization": str(org_id)})
    return org


async def parent_organization(db: AsyncSession, org_id: uuid.UUID) -> Organization | None:
    """One step up ruling 6's chain (agency -> territorial -> leshoz -> bolim ->
    aylanma -> bolak), or `None` at the top.

    Written for 3.9a's over-limit forward (decision #29: «эскалируется
    ваколатли шахсу вышестоящей организации»). It lives here rather than in the
    calling module because reference data is read through `admin`'s own service
    (CLAUDE.md) — a private `SELECT parent_id` in `applications.repo` would be a
    boundary violation however small it looks, and the next caller that needs
    the same step would write a second one.

    **`None` means "this is the root", and nothing else.** An unknown id raises
    404 through `organization_or_404` rather than answering `None`, so a caller
    refusing an escalation with «no parent organization» can never be answering
    a typo instead.

    No archived-parent branch, and that is a fact about the data rather than an
    omission: `archive_organization` refuses to archive a node that still has
    active children, so an active organization cannot have an archived parent.
    """
    org = await organization_or_404(db, org_id)
    if org.parent_id is None:
        return None
    return await repo.get_organization(db, org.parent_id)


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


_ITEM_AUDITED_FIELDS = ("code", "name", "props", "valid_from", "valid_to", "sort_order", "status")


def _item_snapshot(item: ClassifierItem) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for field in _ITEM_AUDITED_FIELDS:
        value = getattr(item, field)
        data[field] = value.isoformat() if isinstance(value, date) else value
    return data


async def create_classifier(db: AsyncSession, *, data: ClassifierIn, actor: User) -> Classifier:
    if await repo.get_classifier_by_code(db, data.code) is not None:
        raise err("ERR-VAL-001", details={"code": data.code, "reason": "already exists"})
    classifier = Classifier(code=data.code, name=data.name.root)
    await repo.add(db, classifier)
    await audit.log(
        db,
        action="classifier.create",
        user_id=actor.id,
        object_type="classifier",
        object_id=classifier.id,
        new_value={"code": classifier.code, "name": classifier.name},
    )
    return classifier


async def add_classifier_item(
    db: AsyncSession, *, classifier_code: str, data: ClassifierItemIn, actor: User
) -> ClassifierItem:
    classifier = await _classifier_or_404(db, classifier_code)
    active = await repo.list_classifier_items(db, classifier.id, include_archived=True)
    if any(row.code == data.code and row.status == "active" for row in active):
        raise err("ERR-VAL-001", details={"code": data.code, "reason": "already active"})
    if data.valid_to is not None and data.valid_to < data.valid_from:
        # Must be caught here, not by the `valid_period` DB CHECK (finding 4): an
        # IntegrityError has no handler in main.py and would surface as ERR-SYS-001.
        raise err("ERR-VAL-001", details={"reason": "valid_to before valid_from"})
    item = ClassifierItem(
        classifier_id=classifier.id,
        code=data.code,
        name=data.name.root,
        props=data.props,
        valid_from=data.valid_from,
        valid_to=data.valid_to,
        sort_order=data.sort_order,
    )
    await repo.add(db, item)
    await audit.log(
        db,
        action="classifier_item.create",
        user_id=actor.id,
        object_type="classifier_item",
        object_id=item.id,
        new_value=_item_snapshot(item),
    )
    return item


async def _item_or_404(db: AsyncSession, item_id: uuid.UUID) -> ClassifierItem:
    item = await repo.get_classifier_item(db, item_id)
    if item is None:
        raise err("ERR-SYS-003", details={"classifier_item": str(item_id)})
    return item


async def update_classifier_item(
    db: AsyncSession, *, item_id: uuid.UUID, patch: ClassifierItemPatch, actor: User
) -> ClassifierItem:
    """Edits presentation only. `code` and `valid_from` are identity/history — changing
    the meaning of a code is a supersede, not an update (ruling 7)."""
    item = await _item_or_404(db, item_id)
    before = _item_snapshot(item)
    fields = patch.model_dump(exclude_unset=True)
    if "name" in fields and patch.name is not None:
        item.name = patch.name.root
    if (
        "valid_to" in fields
        and fields["valid_to"] is not None
        and fields["valid_to"] < item.valid_from
    ):
        # Same reasoning as add_classifier_item: reject before the DB CHECK can (500).
        raise err("ERR-VAL-001", details={"reason": "valid_to before valid_from"})
    for field in ("props", "valid_to", "sort_order"):
        if field in fields:
            setattr(item, field, fields[field])
    await db.flush()
    await audit.log(
        db,
        action="classifier_item.update",
        user_id=actor.id,
        object_type="classifier_item",
        object_id=item.id,
        old_value=before,
        new_value=_item_snapshot(item),
    )
    return item


async def archive_classifier_item(
    db: AsyncSession, *, item_id: uuid.UUID, actor: User, valid_to: date | None = None
) -> ClassifierItem:
    item = await _item_or_404(db, item_id)
    if item.status == "archived":
        return item
    before = _item_snapshot(item)
    item.status = "archived"
    # The default end date is "yesterday" — the same "close the day before" the
    # next thing starts convention `supersede` uses for its archived predecessor
    # (here, the thing that "starts" is the archival itself, today). Clamped to
    # never precede `valid_from`: an item whose `valid_from` is today or later
    # would otherwise compute a `valid_to` before `valid_from` and fail the
    # `valid_period` CHECK at flush (ERR-SYS-001, 500) instead of archiving cleanly.
    item.valid_to = (
        valid_to or item.valid_to or max(item.valid_from, business_today() - timedelta(days=1))
    )
    await db.flush()
    await audit.log(
        db,
        action="classifier_item.archive",
        user_id=actor.id,
        object_type="classifier_item",
        object_id=item.id,
        old_value=before,
        new_value=_item_snapshot(item),
    )
    return item


async def supersede_classifier_item(
    db: AsyncSession, *, item_id: uuid.UUID, data: ClassifierItemIn, actor: User
) -> ClassifierItem:
    """New version of the same code (ruling 7): the old row is closed the day before the
    new one starts and archived, then the new row is inserted — one transaction, so the
    partial unique index never sees two active rows for the same code."""
    old = await _item_or_404(db, item_id)
    if old.status == "archived":
        # The early-return in archive_classifier_item below would silently skip
        # re-closing an already-archived row, leaving its old valid_to in place
        # while this call still inserted an overlapping new one (finding 1,
        # whole-branch review). Adding a new item is the right operation here.
        raise err("ERR-VAL-001", details={"reason": "already archived"})
    if data.code != old.code:
        raise err("ERR-VAL-001", details={"reason": "code must match", "code": old.code})
    if data.valid_from <= (old.valid_to or old.valid_from):
        raise err("ERR-VAL-001", details={"reason": "valid_from must be later"})
    await archive_classifier_item(
        db, item_id=old.id, actor=actor, valid_to=data.valid_from - timedelta(days=1)
    )
    classifier = await db.get(Classifier, old.classifier_id)
    assert classifier is not None  # FK guarantees it
    return await add_classifier_item(db, classifier_code=classifier.code, data=data, actor=actor)


async def list_settings(db: AsyncSession) -> list[SettingOut]:
    rows = {
        row.key: row
        for row in (await db.execute(select(SystemSetting))).scalars()
        if row.key in settings_store.SETTING_SPECS
    }
    out: list[SettingOut] = []
    for key, spec in settings_store.SETTING_SPECS.items():
        out.append(
            SettingOut(
                key=key,
                value=await settings_store.get_setting(db, key),
                default=spec.default,
                description=spec.description,
                overridden=key in rows,
            )
        )
    return out


async def update_setting(db: AsyncSession, *, key: str, raw_value: Any, actor: User) -> SettingOut:
    spec = settings_store.SETTING_SPECS.get(key)
    if spec is None:
        raise err("ERR-SYS-003", details={"setting": key})
    value = settings_store.coerce(spec, raw_value)  # raises ERR-VAL-001 on bad input
    previous = await settings_store.get_setting(db, key)
    row = await db.get(SystemSetting, key)
    if row is None:
        row = SystemSetting(key=key, value=value, description=spec.description)
        db.add(row)
    else:
        row.value = value
    row.updated_by = actor.id
    await db.flush()
    settings_store.invalidate(key)
    await audit.log(
        db,
        action="setting.update",
        user_id=actor.id,
        object_type="system_setting",
        old_value={"value": previous},
        new_value={"value": value},
        basis=key,
    )
    return SettingOut(
        key=key,
        value=value,
        default=spec.default,
        description=spec.description,
        overridden=True,
    )
