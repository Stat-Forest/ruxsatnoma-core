"""Idempotent import of reference DATA (ruling 5): districts and organizations.

Schema-stable catalogs are seeded by migrations; these two are operational data that
change with reorganizations, so they are reloadable without a migration. Every row is
matched by `code`: present → updated, absent → created. Nothing is ever deleted here.
"""

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.modules.admin import repo
from app.modules.admin.models import District, Organization, Region
from app.modules.admin.service import ALLOWED_PARENT_KINDS
from app.modules.audit import service as audit

ENTITIES = ("districts", "organizations")


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Read a JSON array of objects; anything else is a usage error."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise err("ERR-VAL-001", details={"file": str(path), "reason": "expected a JSON array"})
    return data


async def _region_id(db: AsyncSession, code: str | None) -> uuid.UUID | None:
    """Resolve an optional region code (organizations: the field may be absent
    entirely). A present-but-unknown code is still a domain error."""
    if code is None:
        return None
    region_id = (
        await db.execute(select(Region.id).where(Region.code == code))
    ).scalar_one_or_none()
    if region_id is None:
        raise err("ERR-VAL-001", details={"region_code": code, "reason": "unknown region"})
    return region_id


async def _required_region_id(db: AsyncSession, code: str | None) -> uuid.UUID:
    """Resolve a mandatory region code (districts: `District.region_id` is
    non-nullable). A missing or null code is a domain error in its own right,
    distinct from a code that is present but does not resolve — and unlike an
    `assert`, this check runs even under `python -O` and always raises via `err`."""
    if code is None:
        raise err("ERR-VAL-001", details={"reason": "region_code is required"})
    region_id = (
        await db.execute(select(Region.id).where(Region.code == code))
    ).scalar_one_or_none()
    if region_id is None:
        raise err("ERR-VAL-001", details={"region_code": code, "reason": "unknown region"})
    return region_id


async def _district_id(db: AsyncSession, code: str | None) -> uuid.UUID | None:
    if code is None:
        return None
    district_id = (
        await db.execute(select(District.id).where(District.code == code))
    ).scalar_one_or_none()
    if district_id is None:
        raise err("ERR-VAL-001", details={"district_code": code, "reason": "unknown district"})
    return district_id


async def seed_districts(db: AsyncSession, rows: list[dict[str, Any]]) -> tuple[int, int]:
    """`region_code` is required on every row (`District.region_id` is non-nullable).
    `soato_code`/`sort_order` preserve the existing value when their key is absent
    from a row being updated (the general preserve-on-absence rule is documented on
    `seed_organizations`, which has more fields it applies to)."""
    created = updated = 0
    for row in rows:
        region_id = await _required_region_id(db, row.get("region_code"))
        existing = (
            await db.execute(select(District).where(District.code == row["code"]))
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                District(
                    code=row["code"],
                    soato_code=row.get("soato_code"),
                    name=row["name"],
                    region_id=region_id,
                    sort_order=row.get("sort_order", 0),
                )
            )
            created += 1
        else:
            existing.soato_code = row.get("soato_code", existing.soato_code)
            existing.name = row["name"]
            existing.region_id = region_id
            existing.sort_order = row.get("sort_order", existing.sort_order)
            updated += 1
        await db.flush()
    await audit.log(
        db,
        action="reference.seed",
        object_type="district",
        basis="seed CLI",
        extra={"created": created, "updated": updated},
    )
    return created, updated


async def seed_organizations(db: AsyncSession, rows: list[dict[str, Any]]) -> tuple[int, int]:
    """Rows are applied in file order: a parent must appear before its children (or
    already exist in the DB). Kind pairing and the archived-parent rule are validated
    against the same source of truth `admin.service` enforces for the write API
    (`ALLOWED_PARENT_KINDS`, and a `status != "active"` parent), so a bad file cannot
    build a hierarchy the API would refuse.

    On update, `stir`/`region_code`/`district_code`/`requisites` are
    preserve-on-absence (ruling 5: reorganizations are partial re-edits of the file,
    not full re-descriptions): a key missing from the row leaves the stored value
    untouched; a key present with JSON `null` clears it; a key present with a value
    sets it. `name` and `kind` are always required and always overwrite.
    """
    created = updated = 0
    for row in rows:
        kind = row["kind"]
        allowed = ALLOWED_PARENT_KINDS.get(kind)
        if allowed is None:
            raise err("ERR-VAL-001", details={"kind": kind, "reason": "unknown kind"})
        parent_code = row.get("parent_code")
        parent: Organization | None = None
        if parent_code is not None:
            parent = await repo.get_organization_by_code(db, parent_code)
            if parent is None:
                raise err(
                    "ERR-VAL-001",
                    details={"parent_code": parent_code, "reason": "unknown parent"},
                )
        if not allowed:
            if parent is not None:
                raise err("ERR-VAL-001", details={"kind": kind, "reason": "must be root"})
            existing_root = await repo.get_agency(db)
            if existing_root is not None and existing_root.code != row["code"]:
                raise err(
                    "ERR-VAL-001",
                    details={"kind": kind, "reason": "root already exists"},
                )
        elif parent is None:
            raise err("ERR-VAL-001", details={"kind": kind, "reason": "parent required"})
        elif parent.kind not in allowed:
            raise err(
                "ERR-VAL-001",
                details={"kind": kind, "parent_kind": parent.kind, "allowed": list(allowed)},
            )
        elif parent.status != "active":
            raise err("ERR-VAL-001", details={"reason": "parent archived"})

        existing = await repo.get_organization_by_code(db, row["code"])
        if existing is None:
            region_id = await _region_id(db, row.get("region_code"))
            district_id = await _district_id(db, row.get("district_code"))
            db.add(
                Organization(
                    code=row["code"],
                    kind=kind,
                    parent_id=parent.id if parent is not None else None,
                    name=row["name"],
                    stir=row.get("stir"),
                    region_id=region_id,
                    district_id=district_id,
                    requisites=row.get("requisites", {}),
                )
            )
            created += 1
        else:
            existing.parent_id = parent.id if parent is not None else None
            existing.name = row["name"]
            existing.stir = row.get("stir", existing.stir)
            if "region_code" in row:
                existing.region_id = await _region_id(db, row["region_code"])
            if "district_code" in row:
                existing.district_id = await _district_id(db, row["district_code"])
            existing.requisites = row.get("requisites", existing.requisites)
            updated += 1
        await db.flush()
    await audit.log(
        db,
        action="reference.seed",
        object_type="organization",
        basis="seed CLI",
        extra={"created": created, "updated": updated},
    )
    return created, updated


async def run(entity: str, rows: list[dict[str, Any]], db: AsyncSession) -> str:
    if entity not in ENTITIES:
        raise err("ERR-VAL-001", details={"entity": entity, "allowed": list(ENTITIES)})
    seeder = seed_districts if entity == "districts" else seed_organizations
    created, updated = await seeder(db, rows)
    return f"{entity}: {created} created, {updated} updated"
