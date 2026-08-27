"""admin DDL: CHECKs, partial unique indexes, hierarchy rules, closed user FKs."""

import uuid
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.models import SystemSetting
from app.modules.admin.models import (
    ActivityType,
    Classifier,
    ClassifierItem,
    District,
    LivestockType,
    Organization,
    Region,
)
from app.modules.auth.models import Role, User


async def make_region(db, code: str = "test-region") -> Region:
    region = Region(code=code, name={"uz_cyrl": "Тест", "en": "Test"})
    db.add(region)
    await db.flush()
    return region


async def make_agency(db) -> Organization:
    """Get-or-create the single root (ruling 6 allows exactly one `agency` row).

    API tests elsewhere commit their rows, so the agency may already exist — never
    assume an empty table.
    """
    existing = (
        await db.execute(select(Organization).where(Organization.kind == "agency"))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    org = Organization(kind="agency", code="agency", name={"uz_cyrl": "Агентлик", "en": "Agency"})
    db.add(org)
    await db.flush()
    return org


async def test_region_and_district(db):
    region = await make_region(db, "region-a")
    db.add(District(code="district-a", name={"uz_cyrl": "Туман"}, region_id=region.id))
    await db.flush()
    row = (await db.execute(select(District).where(District.code == "district-a"))).scalar_one()
    assert row.region_id == region.id
    assert row.soato_code is None  # filled at stage 7.2 (ruling 4)


async def test_district_requires_existing_region(db):
    db.add(District(code="orphan", name={"uz_cyrl": "Туман"}, region_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_organization_kind_check(db):
    db.add(Organization(kind="ministry", code="bad-kind", name={"uz_cyrl": "Х"}))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_only_agency_may_be_root(db):
    db.add(Organization(kind="leshoz", code="rootless", name={"uz_cyrl": "Х"}))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_agency_may_not_have_parent(db):
    root = await make_agency(db)
    db.add(
        Organization(kind="agency", code="second-agency", name={"uz_cyrl": "Х"}, parent_id=root.id)
    )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_single_agency_row(db):
    await make_agency(db)
    db.add(Organization(kind="agency", code="agency-two", name={"uz_cyrl": "Х"}))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_organization_stir_format(db):
    root = await make_agency(db)
    db.add(
        Organization(
            kind="leshoz", code="bad-stir", name={"uz_cyrl": "Х"}, parent_id=root.id, stir="12345"
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_classifier_item_active_code_unique(db):
    classifier = Classifier(code="test_reasons", name={"uz_cyrl": "Сабаблар"})
    db.add(classifier)
    await db.flush()
    first = ClassifierItem(
        classifier_id=classifier.id,
        code="X-01",
        name={"uz_cyrl": "Биринчи"},
        valid_from=date(2026, 1, 1),
    )
    db.add(first)
    await db.flush()

    db.add(
        ClassifierItem(
            classifier_id=classifier.id,
            code="X-01",
            name={"uz_cyrl": "Иккинчи"},
            valid_from=date(2026, 6, 1),
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()

    # After archiving the old row the same code may be re-issued (ruling 7)
    classifier = Classifier(code="test_reasons2", name={"uz_cyrl": "Сабаблар"})
    db.add(classifier)
    await db.flush()
    db.add(
        ClassifierItem(
            classifier_id=classifier.id,
            code="X-01",
            name={"uz_cyrl": "Эски"},
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 5, 31),
            status="archived",
        )
    )
    db.add(
        ClassifierItem(
            classifier_id=classifier.id,
            code="X-01",
            name={"uz_cyrl": "Янги"},
            valid_from=date(2026, 6, 1),
        )
    )
    await db.flush()  # no violation: only one active row per (classifier, code)


async def test_classifier_item_period_check(db):
    classifier = Classifier(code="test_period", name={"uz_cyrl": "Х"})
    db.add(classifier)
    await db.flush()
    db.add(
        ClassifierItem(
            classifier_id=classifier.id,
            code="P-01",
            name={"uz_cyrl": "Х"},
            valid_from=date(2026, 6, 1),
            valid_to=date(2026, 1, 1),
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_activity_type_quantity_unit_check(db):
    db.add(
        ActivityType(
            code="test_activity", name={"uz_cyrl": "Х"}, quantity_unit="litre", sort_order=99
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_livestock_type_status_check(db):
    db.add(LivestockType(code="test_beast", name={"uz_cyrl": "Х"}, status="ghost"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_system_setting_roundtrip(db):
    db.add(SystemSetting(key="test.key", value={"n": 5}, description="test"))
    await db.flush()
    row = await db.get(SystemSetting, "test.key")
    assert row is not None and row.value == {"n": 5}
    assert row.updated_by is None


async def test_user_organization_fk_enforced(db):
    """Closes 3.2a ruling 4: users.organization_id/region_id/district_id are real FKs now."""
    role_id = (await db.execute(select(Role.id).where(Role.code == "sys_admin"))).scalar_one()
    db.add(User(full_name="Ghost org", role_id=role_id, organization_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_user_region_fk_enforced(db):
    role_id = (await db.execute(select(Role.id).where(Role.code == "sys_admin"))).scalar_one()
    db.add(User(full_name="Ghost region", role_id=role_id, region_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        await db.flush()
