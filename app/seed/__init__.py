"""Idempotent import of reference DATA (ruling 5): districts and organizations.

Schema-stable catalogs are seeded by migrations; these two are operational data that
change with reorganizations, so they are reloadable without a migration. Every row is
matched by `code`: present → updated, absent → created. Nothing is ever deleted here.
"""

import json
import re
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.schemas import LocalizedName
from app.modules.admin import repo
from app.modules.admin.models import District, Organization, Region
from app.modules.admin.service import ALLOWED_PARENT_KINDS
from app.modules.audit import service as audit

ENTITIES = ("districts", "organizations")

# Matches the `stir_format` DB CHECK (admin.schemas.Stir) — validated here too so a
# bad file 422-equivalents (ERR-VAL-001) instead of an IntegrityError traceback.
# [0-9], not \d: \d is Unicode-aware in Python's re, so it would accept e.g. nine
# Arabic-Indic digits that the ASCII-only Postgres CHECK rejects.
_STIR_RE = re.compile(r"^[0-9]{9}$")


def _validated_name(code: str, raw: Any) -> dict[str, Any]:
    """The write API enforces `LocalizedName` (ruling 13: `uz_latn` required since
    decision #90, only known locales) via Pydantic; the seed CLI bypassed that and
    stored `name` as free-form JSONB — a file with `"name": "Нукус"` or a stray
    locale key would be written and then break `/refs` on read."""
    try:
        return LocalizedName.model_validate(raw).root
    except ValidationError as exc:
        raise err(
            "ERR-VAL-001", details={"code": code, "reason": "invalid name", "errors": str(exc)}
        ) from exc


def _validated_stir(code: str, stir: str | None) -> str | None:
    if stir is not None and not _STIR_RE.fullmatch(stir):
        raise err("ERR-VAL-001", details={"code": code, "reason": "invalid stir"})
    return stir


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
        name = _validated_name(row["code"], row["name"])
        existing = (
            await db.execute(select(District).where(District.code == row["code"]))
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                District(
                    code=row["code"],
                    soato_code=row.get("soato_code"),
                    name=name,
                    region_id=region_id,
                    sort_order=row.get("sort_order", 0),
                )
            )
            created += 1
        else:
            existing.soato_code = row.get("soato_code", existing.soato_code)
            existing.name = name
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
    already exist in the DB). Kind pairing, re-parent cycles and the archived-parent
    rule are validated against the same source of truth `admin.service` enforces for
    the write API (`ALLOWED_PARENT_KINDS`, `admin.repo.is_descendant`, and a
    `status != "active"` parent), so a bad file cannot build a hierarchy the API
    would refuse. `name`/`stir`/`requisites` are validated the same way the write API
    validates them (`LocalizedName`, the `^[0-9]{9}$` STIR shape, requisites must be an
    object) — a bad file must not be able to store something `/refs` later fails to
    read, or a CHECK constraint catches as an unhandled `IntegrityError`.

    On update, `stir`/`region_code`/`district_code`/`requisites` are
    preserve-on-absence (ruling 5: reorganizations are partial re-edits of the file,
    not full re-descriptions): a key missing from the row leaves the stored value
    untouched; a key present with JSON `null` clears it (`requisites` excepted: it
    must be an object whenever the key is present, so `null` is rejected like any
    other non-object value). `name` is always required and always overwrites.
    `kind` is always required, but a row that CHANGES an existing code's kind is
    rejected outright — moving an organization between hierarchy levels is too
    consequential for a bulk importer to do silently.
    """
    created = updated = 0
    for row in rows:
        code = row["code"]
        kind = row["kind"]
        allowed = ALLOWED_PARENT_KINDS.get(kind)
        if allowed is None:
            raise err("ERR-VAL-001", details={"kind": kind, "reason": "unknown kind"})

        existing = await repo.get_organization_by_code(db, code)
        if existing is not None and existing.kind != kind:
            raise err(
                "ERR-VAL-001", details={"code": code, "reason": "kind change is not supported"}
            )

        parent_code = row.get("parent_code")
        parent: Organization | None = None
        if parent_code is not None:
            parent = await repo.get_organization_by_code(db, parent_code)
            if parent is None:
                raise err(
                    "ERR-VAL-001",
                    details={"parent_code": parent_code, "reason": "unknown parent"},
                )

        if existing is not None and parent is not None:
            # Mirrors admin.service.update_organization (ruling 6), including running
            # BEFORE the kind/parent-pairing check below: a cycle is a cycle
            # regardless of whether the candidate parent's kind would otherwise be
            # an allowed one. A plain FK has no notion of a cycle to catch this.
            if parent.id == existing.id or await repo.is_descendant(
                db, ancestor_id=existing.id, candidate_id=parent.id
            ):
                raise err("ERR-VAL-001", details={"reason": "cycle"})

        if not allowed:
            if parent is not None:
                raise err("ERR-VAL-001", details={"kind": kind, "reason": "must be root"})
            existing_root = await repo.get_agency(db)
            if existing_root is not None and existing_root.code != code:
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

        name = _validated_name(code, row["name"])
        if "requisites" in row and not isinstance(row["requisites"], dict):
            raise err(
                "ERR-VAL-001", details={"code": code, "reason": "requisites must be an object"}
            )
        # Ruling #178: whether this organization has GIS layers at all. The
        # Agency says most leshozes do not and gave no date, so this is the
        # common case in a real import file rather than an exotic one — and a
        # bulk importer that cannot express it would force every such leshoz
        # to be edited by hand through the API afterwards. Preserve-on-absence
        # like `stir`/`requisites` (ruling 5), and strictly boolean when
        # present: a string "false" is exactly the shape that would silently
        # enable a map for a leshoz that has none.
        if "gis_enabled" in row and not isinstance(row["gis_enabled"], bool):
            raise err(
                "ERR-VAL-001", details={"code": code, "reason": "gis_enabled must be a boolean"}
            )
        default_stir = None if existing is None else existing.stir
        stir = _validated_stir(code, row.get("stir", default_stir))

        if existing is None:
            region_id = await _region_id(db, row.get("region_code"))
            district_id = await _district_id(db, row.get("district_code"))
            db.add(
                Organization(
                    code=code,
                    kind=kind,
                    parent_id=parent.id if parent is not None else None,
                    name=name,
                    stir=stir,
                    region_id=region_id,
                    district_id=district_id,
                    requisites=row.get("requisites", {}),
                    gis_enabled=row.get("gis_enabled", True),
                )
            )
            created += 1
        else:
            existing.parent_id = parent.id if parent is not None else None
            existing.name = name
            existing.stir = stir
            if "region_code" in row:
                existing.region_id = await _region_id(db, row["region_code"])
            if "district_code" in row:
                existing.district_id = await _district_id(db, row["district_code"])
            existing.requisites = row.get("requisites", existing.requisites)
            existing.gis_enabled = row.get("gis_enabled", existing.gis_enabled)
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
