"""admin reference seeds

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-28 00:00:00.000000

Seeds only what is schema-stable (ruling 5): the 14 regions, the 6 activity types,
the 10 livestock groups (tz/06), the classifier catalog and the RJ-01…RJ-15 reasons
(tz/10 § 8.2). Districts (~208) and organizations arrive via `python -m app.seed`.
Ids are fixed so that fixtures, later migrations and exports can reference them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REGIONS = [
    (
        "0198f100-0000-7000-8000-000000000001",
        "karakalpakstan",
        "Қорақалпоғистон Республикаси",
        "Republic of Karakalpakstan",
        10,
    ),
    ("0198f100-0000-7000-8000-000000000002", "andijan", "Андижон вилояти", "Andijan region", 20),
    ("0198f100-0000-7000-8000-000000000003", "bukhara", "Бухоро вилояти", "Bukhara region", 30),
    ("0198f100-0000-7000-8000-000000000004", "jizzakh", "Жиззах вилояти", "Jizzakh region", 40),
    (
        "0198f100-0000-7000-8000-000000000005",
        "kashkadarya",
        "Қашқадарё вилояти",
        "Kashkadarya region",
        50,
    ),
    ("0198f100-0000-7000-8000-000000000006", "navoiy", "Навоий вилояти", "Navoiy region", 60),
    ("0198f100-0000-7000-8000-000000000007", "namangan", "Наманган вилояти", "Namangan region", 70),
    (
        "0198f100-0000-7000-8000-000000000008",
        "samarkand",
        "Самарқанд вилояти",
        "Samarkand region",
        80,
    ),
    (
        "0198f100-0000-7000-8000-000000000009",
        "surkhandarya",
        "Сурхондарё вилояти",
        "Surkhandarya region",
        90,
    ),
    ("0198f100-0000-7000-8000-00000000000a", "syrdarya", "Сирдарё вилояти", "Syrdarya region", 100),
    (
        "0198f100-0000-7000-8000-00000000000b",
        "tashkent-region",
        "Тошкент вилояти",
        "Tashkent region",
        110,
    ),
    ("0198f100-0000-7000-8000-00000000000c", "fergana", "Фарғона вилояти", "Fergana region", 120),
    ("0198f100-0000-7000-8000-00000000000d", "khorezm", "Хоразм вилояти", "Khorezm region", 130),
    (
        "0198f100-0000-7000-8000-00000000000e",
        "tashkent-city",
        "Тошкент шаҳри",
        "Tashkent city",
        140,
    ),
]

# quantity_unit values are provisional until VMQ 278 is received (ruling 14)
ACTIVITY_TYPES = [
    (
        "0198f100-0001-7000-8000-000000000001",
        "grazing",
        "Чорва молларини боқиш",
        "Livestock grazing",
        "head",
        10,
    ),
    ("0198f100-0001-7000-8000-000000000002", "haymaking", "Пичан тайёрлаш", "Haymaking", "ton", 20),
    ("0198f100-0001-7000-8000-000000000003", "apiary", "Асаларичилик", "Apiary", "hive", 30),
    (
        "0198f100-0001-7000-8000-000000000004",
        "recreation",
        "Дам олиш ва туризм",
        "Recreation and tourism",
        "ha",
        40,
    ),
    (
        "0198f100-0001-7000-8000-000000000005",
        "deadwood",
        "Қуруқ шох-шабба йиғиш",
        "Deadwood collection",
        "ton",
        50,
    ),
    (
        "0198f100-0001-7000-8000-000000000006",
        "science",
        "Илмий тадқиқот",
        "Scientific research",
        "ha",
        60,
    ),
]

# tz/06: adults, young under 2, sheep/goats 6m+, lambs/kids under 6m
LIVESTOCK_TYPES = [
    (
        "0198f100-0002-7000-8000-000000000001",
        "cattle_adult",
        "Қорамол (катта)",
        "Cattle, adult",
        10,
    ),
    (
        "0198f100-0002-7000-8000-000000000002",
        "cattle_young",
        "Қорамол (2 ёшгача)",
        "Cattle, under 2 years",
        20,
    ),
    ("0198f100-0002-7000-8000-000000000003", "horse_adult", "От (катта)", "Horse, adult", 30),
    (
        "0198f100-0002-7000-8000-000000000004",
        "horse_young",
        "От (2 ёшгача)",
        "Horse, under 2 years",
        40,
    ),
    ("0198f100-0002-7000-8000-000000000005", "camel_adult", "Туя (катта)", "Camel, adult", 50),
    (
        "0198f100-0002-7000-8000-000000000006",
        "camel_young",
        "Туя (2 ёшгача)",
        "Camel, under 2 years",
        60,
    ),
    ("0198f100-0002-7000-8000-000000000007", "donkey_adult", "Эшак (катта)", "Donkey, adult", 70),
    (
        "0198f100-0002-7000-8000-000000000008",
        "donkey_young",
        "Эшак (2 ёшгача)",
        "Donkey, under 2 years",
        80,
    ),
    (
        "0198f100-0002-7000-8000-000000000009",
        "sheep_goat_6m",
        "Қўй ва эчки (6 ойдан катта)",
        "Sheep and goats, 6 months and older",
        90,
    ),
    (
        "0198f100-0002-7000-8000-00000000000a",
        "lamb_kid_under_6m",
        "Қўзи ва улоқ (6 ойгача)",
        "Lambs and kids, under 6 months",
        100,
    ),
]

CLASSIFIERS = [
    (
        "0198f100-0003-7000-8000-000000000001",
        "rejection_reasons",
        "Рад этиш ва қайтариш сабаблари",
        "Rejection and return reasons",
    ),
    ("0198f100-0003-7000-8000-000000000002", "doc_types", "Ҳужжат турлари", "Document types"),
    (
        "0198f100-0003-7000-8000-000000000003",
        "benefit_categories",
        "Имтиёз тоифалари",
        "Benefit categories",
    ),
    (
        "0198f100-0003-7000-8000-000000000004",
        "violation_types",
        "Қоидабузарлик турлари",
        "Violation types",
    ),
    (
        "0198f100-0003-7000-8000-000000000005",
        "appeal_subjects",
        "Мурожаат мавзулари",
        "Appeal subjects",
    ),
]

# tz/10 § 8.2. kind: return / reject / cancel; RJ-15 covers both, recorded as "reject".
REJECTION_REASONS = [
    (
        "RJ-01",
        "Ҳужжатлар тўлиқ эмас ёки талабларга жавоб бермайди",
        "Documents incomplete or non-compliant",
        "return",
        "ВМҚ 290",
    ),
    (
        "RJ-02",
        "Ариза берувчи маълумотлари нотўғри ёки тасдиқланмаган",
        "Applicant data incorrect or unconfirmed",
        "return",
        "ВМҚ 290",
    ),
    (
        "RJ-03",
        "Участка ўрмон фонди чегарасидан ташқарида",
        "Plot outside the forest fund",
        "reject",
        "Закон «Ўрмон тўғрисида»",
    ),
    (
        "RJ-04",
        "Майдон бошқа амалдаги рухсатнома билан банд",
        "Area occupied by another active permit",
        "reject",
        "ВМҚ 689",
    ),
    ("RJ-05", "Юклама нормадан ошиб кетган", "Load exceeds the norm", "reject", "ВМҚ 689"),
    (
        "RJ-06",
        "Контурда тасдиқланган норма йўқ",
        "No approved norm for the contour",
        "reject",
        "ВМҚ 689",
    ),
    (
        "RJ-07",
        "Мавсум ёки ротацияга мос эмас",
        "Does not match the season or rotation",
        "reject",
        "ВМҚ 689",
    ),
    (
        "RJ-08",
        "Участка муҳофаза этиладиган ёки чекланган ҳудудда",
        "Plot in a protected or restricted zone",
        "reject",
        "Закон «Ўрмон тўғрисида»",
    ),
    ("RJ-09", "Ёнғин тақиқи даври", "Fire-ban period", "reject", "ВМҚ 506"),
    (
        "RJ-10",
        "Ветеринария талаблари бажарилмаган",
        "Veterinary requirements not met",
        "reject",
        "вет. законодательство",
    ),
    (
        "RJ-11",
        "Тўлов муддатида амалга оширилмаган",
        "Payment not made in time",
        "reject",
        "ВМҚ 278",
    ),
    (
        "RJ-12",
        "Ариза берувчида бартараф этилмаган қоидабузарлик бор",
        "Applicant has an unresolved violation",
        "reject",
        "Закон «Ўрмон тўғрисида»",
    ),
    ("RJ-13", "Ариза берувчининг мурожаатига кўра", "At the applicant's request", "cancel", ""),
    (
        "RJ-14",
        "Ваколатли орган ёки суд қарорига кўра",
        "By decision of an authorized body or court",
        "cancel",
        "соответствующее решение",
    ),
    ("RJ-15", "Бошқа (изоҳ мажбурий)", "Other (comment required)", "reject", ""),
]


def upgrade() -> None:
    """Insert reference rows. asyncpg needs explicit uuid casts for text binds."""
    for row_id, code, name_cyr, name_en, sort_order in REGIONS:
        op.execute(
            sa.text(
                "INSERT INTO regions (id, code, name, sort_order) VALUES "
                "(CAST(:id AS uuid), :code, jsonb_build_object('uz_cyrl', :cyr, 'en', :en), :sort)"
            ).bindparams(id=row_id, code=code, cyr=name_cyr, en=name_en, sort=sort_order)
        )

    for row_id, code, name_cyr, name_en, unit, sort_order in ACTIVITY_TYPES:
        op.execute(
            sa.text(
                "INSERT INTO activity_types (id, code, name, quantity_unit, sort_order, status) "
                "VALUES (CAST(:id AS uuid), :code, "
                "jsonb_build_object('uz_cyrl', :cyr, 'en', :en), :unit, :sort, 'active')"
            ).bindparams(id=row_id, code=code, cyr=name_cyr, en=name_en, unit=unit, sort=sort_order)
        )

    for row_id, code, name_cyr, name_en, sort_order in LIVESTOCK_TYPES:
        op.execute(
            sa.text(
                "INSERT INTO livestock_types (id, code, name, sort_order, status) VALUES "
                "(CAST(:id AS uuid), :code, "
                "jsonb_build_object('uz_cyrl', :cyr, 'en', :en), :sort, 'active')"
            ).bindparams(id=row_id, code=code, cyr=name_cyr, en=name_en, sort=sort_order)
        )

    for row_id, code, name_cyr, name_en in CLASSIFIERS:
        op.execute(
            sa.text(
                "INSERT INTO classifiers (id, code, name) VALUES "
                "(CAST(:id AS uuid), :code, jsonb_build_object('uz_cyrl', :cyr, 'en', :en))"
            ).bindparams(id=row_id, code=code, cyr=name_cyr, en=name_en)
        )

    reasons_id = CLASSIFIERS[0][0]
    for index, (code, name_cyr, name_en, kind, legal_basis) in enumerate(REJECTION_REASONS, 1):
        op.execute(
            sa.text(
                "INSERT INTO classifier_items "
                "(id, classifier_id, code, name, props, valid_from, sort_order, status) VALUES "
                "(gen_random_uuid(), CAST(:classifier_id AS uuid), :code, "
                "jsonb_build_object('uz_cyrl', :cyr, 'en', :en), "
                "jsonb_build_object('kind', :kind, 'legal_basis', :basis), "
                "DATE '2026-01-01', :sort, 'active')"
            ).bindparams(
                classifier_id=reasons_id,
                code=code,
                cyr=name_cyr,
                en=name_en,
                kind=kind,
                basis=legal_basis,
                sort=index * 10,
            )
        )


def downgrade() -> None:
    reasons_id = CLASSIFIERS[0][0]
    op.execute(
        sa.text("DELETE FROM classifier_items WHERE classifier_id = CAST(:id AS uuid)").bindparams(
            id=reasons_id
        )
    )
    for table, rows in (
        ("classifiers", CLASSIFIERS),
        ("livestock_types", LIVESTOCK_TYPES),
        ("activity_types", ACTIVITY_TYPES),
        ("regions", REGIONS),
    ):
        codes = [row[1] for row in rows]
        op.execute(sa.text(f"DELETE FROM {table} WHERE code = ANY(:codes)").bindparams(codes=codes))
