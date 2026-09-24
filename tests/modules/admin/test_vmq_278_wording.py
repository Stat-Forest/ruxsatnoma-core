"""Reference wording migration 0065 brings level with VMQ 278 as lex.uz publishes it.

Two facts of the decree that the seeds had missed: VMQ 271 of 21.05.2026 renamed the
"Muruvvat" homes in ¶12 to "G'amxo'rlik" centres, and the tariff table charges nothing
for suckling young — which the official grazing blank repeats under its head total.
"""

from sqlalchemy import select

from app.modules.admin.models import Classifier, ClassifierItem, LivestockType

SUCKLING_EXCLUDED = {
    "cattle_young",
    "horse_young",
    "camel_young",
    "donkey_young",
    "lamb_kid_under_6m",
}
# The ten codes 0005 seeds — other tests commit their own livestock rows, so the
# table itself is never closed to these.
SEEDED = SUCKLING_EXCLUDED | {
    "cattle_adult",
    "horse_adult",
    "camel_adult",
    "donkey_adult",
    "sheep_goat_6m",
}
# The exclusion as each seeded language words it — the blank's own phrase in Uzbek.
SUCKLING_PHRASE = {"uz_latn": "ona suti", "uz_cyrl": "она сути", "en": "suckling"}


async def test_the_orphanage_benefit_names_the_gamxorlik_centres(db):
    item = (
        await db.execute(
            select(ClassifierItem)
            .join(Classifier, Classifier.id == ClassifierItem.classifier_id)
            .where(
                Classifier.code == "benefit_categories",
                ClassifierItem.code == "orphanage_residents",
                ClassifierItem.status == "active",
            )
        )
    ).scalar_one()

    assert "Gʻamxoʻrlik" in item.name["uz_latn"]
    assert "Ғамхўрлик" in item.name["uz_cyrl"]
    assert "Гамхурлик" in item.name["ru"]
    for text in item.name.values():
        assert "uruvvat" not in text and "урувват" not in text
    assert "271" in item.props["basis"]


async def test_young_livestock_labels_say_suckling_young_are_not_counted(db):
    rows = (
        (await db.execute(select(LivestockType).where(LivestockType.code.in_(SEEDED))))
        .scalars()
        .all()
    )
    by_code = {row.code: row for row in rows}
    assert by_code.keys() == SEEDED

    for code, row in by_code.items():
        for lang, phrase in SUCKLING_PHRASE.items():
            mentioned = phrase in row.name[lang]
            assert mentioned == (code in SUCKLING_EXCLUDED), (code, lang, row.name[lang])
