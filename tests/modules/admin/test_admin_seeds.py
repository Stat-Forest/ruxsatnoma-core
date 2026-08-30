"""Reference rows shipped by migration 0005 (ruling 5: regions yes, districts/orgs no)."""

from datetime import date
from pathlib import Path

from sqlalchemy import select

from app.modules.admin.models import (
    ActivityType,
    Classifier,
    ClassifierItem,
    LivestockType,
    Region,
)


async def test_fourteen_regions_seeded(db):
    codes = set((await db.execute(select(Region.code))).scalars())
    assert len(codes) == 14
    assert {"karakalpakstan", "tashkent-city", "fergana"} <= codes
    row = (await db.execute(select(Region).where(Region.code == "karakalpakstan"))).scalar_one()
    assert row.name["uz_cyrl"] == "Қорақалпоғистон Республикаси"
    assert row.soato_code is None  # ruling 4: filled at stage 7.2


def test_districts_are_not_seeded_by_the_migration():
    """Districts come from the seed CLI (ruling 5). A DB count would be wrong here —
    other tests commit districts — so assert against the migration source itself."""
    source = (
        Path(__file__).resolve().parents[3] / "migrations" / "versions" / "0005_admin_seeds.py"
    ).read_text(encoding="utf-8")
    assert "INSERT INTO districts" not in source
    assert "INSERT INTO organizations" not in source


async def test_six_activity_types(db):
    rows = (await db.execute(select(ActivityType).order_by(ActivityType.sort_order))).scalars()
    by_code = {r.code: r for r in rows}
    assert set(by_code) == {
        "grazing",
        "haymaking",
        "apiary",
        "recreation",
        "deadwood",
        "science",
    }
    assert by_code["grazing"].quantity_unit == "head"
    # Migration 0012 (stage 3.7 Task 2) corrects this to the real VMQ 278 unit.
    assert by_code["haymaking"].quantity_unit == "ha"
    assert by_code["apiary"].quantity_unit == "hive"


async def test_ten_livestock_types(db):
    codes = set((await db.execute(select(LivestockType.code))).scalars())
    assert codes == {
        "cattle_adult",
        "cattle_young",
        "horse_adult",
        "horse_young",
        "camel_adult",
        "camel_young",
        "donkey_adult",
        "donkey_young",
        "sheep_goat_6m",
        "lamb_kid_under_6m",
    }


async def test_classifier_catalog(db):
    """Containment, not equality: API tests (e.g. test_refs_api.py) legitimately
    commit their own ad-hoc classifiers, so the table is not closed to these five —
    only migration 0005's seeded codes are guaranteed to be present."""
    codes = set((await db.execute(select(Classifier.code))).scalars())
    assert {
        "rejection_reasons",
        "doc_types",
        "benefit_categories",
        "violation_types",
        "appeal_subjects",
    } <= codes


async def test_rejection_reasons_seeded(db):
    classifier_id = (
        await db.execute(select(Classifier.id).where(Classifier.code == "rejection_reasons"))
    ).scalar_one()
    rows = (
        (
            await db.execute(
                select(ClassifierItem)
                .where(ClassifierItem.classifier_id == classifier_id)
                .order_by(ClassifierItem.sort_order)
            )
        )
        .scalars()
        .all()
    )
    assert [r.code for r in rows] == [f"RJ-{i:02d}" for i in range(1, 16)]
    assert all(r.status == "active" and r.valid_from == date(2026, 1, 1) for r in rows)
    # props carry the decision type and the legal basis (design/02 § classifier_items)
    assert rows[0].props["kind"] == "return"
    assert rows[2].props["kind"] == "reject"
    assert rows[12].props["kind"] == "cancel"
    assert rows[3].props["legal_basis"] == "ВМҚ 689"
