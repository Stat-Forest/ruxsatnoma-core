"""VMQ 278 wording: the Gʻamxoʻrlik centres and suckling young

Two places where the seeded reference data had fallen behind VMQ 278 as lex.uz
publishes it (checked 2026-09-24). Both are PRESENTATION edits made in place —
`admin.service.update_classifier_item`'s ruling 7: a name is presentation, and only
changing what a code MEANS is a supersede. Neither code changes meaning here.

  (a) `benefit_categories/orphanage_residents` (seeded by `0053`). VMQ 271 of
      21.05.2026 (in force 22.05.2026) renamed the "Muruvvat" boarding homes to
      "Gʻamxoʻrlik" centres across the acts that name them, ¶12 of VMQ 278 among
      them. Same people, new institution name — so the name and the `basis` are
      rewritten, the code and every application pointing at the row stay.

  (b) The five young-livestock types (seeded by `0005`, `uz_latn` by `0032`). The
      decree's tariff table charges nothing for young fed on their mother's milk, and
      the official grazing blank prints the same exclusion under its head total
      (`permits/assets/blanks/grazing.html`) — naming the horse foal too, which the
      decree's own list omits. The blank is the document the permit is signed on, so
      all five labels carry it: an applicant counting from the picker must arrive at
      the total the printed caption describes. No `ru` key is added — these rows
      never had one, and the landing supplies its own `ru`/`kaa` fallback.

Every UPDATE must touch exactly one row; anything else means this database did not
run the chain the revision depends on, and it raises rather than skipping silently.
The downgrade writes the previous wording back verbatim.

Revision ID: 0065
Revises: 0061
Create Date: 2026-09-24 21:30:00.000000

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0065"
down_revision: str | Sequence[str] | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- (a) the orphanage benefit --------------------------------------------------------
ORPHANAGE_CODE = "orphanage_residents"
ORPHANAGE_NEW = {
    "name": {
        "uz_latn": '"Mehribonlik" uylari tarbiyalanuvchilari va "Gʻamxoʻrlik" '
        "markazlariga joylashtirilgan shaxslar",
        "uz_cyrl": "«Меҳрибонлик» уйлари тарбияланувчилари ва «Ғамхўрлик» "
        "марказларига жойлаштирилган шахслар",
        "ru": "Воспитанники домов «Мехрибонлик» и лица, помещённые в центры «Гамхурлик»",
    },
    "basis": "ВМҚ 278-сон, 30.09.2015, §IV ¶12 (ВМҚ 271-сон, 21.05.2026 таҳририда)",
}
# `0053`'s own seed, restored verbatim by the downgrade.
ORPHANAGE_OLD = {
    "name": {
        "uz_latn": '"Mehribonlik" va "Muruvvat" tarbiyalanuvchilari',
        "uz_cyrl": "«Меҳрибонлик» ва «Мурувват» тарбияланувчилари",
        "ru": "Воспитанники «Мехрибонлик» и «Мурувват»",
    },
    "basis": "ВМҚ 278-сон, 30.09.2015, §IV ¶12",
}

# --- (b) young livestock ---------------------------------------------------------------
# code -> (new name, name as `0005`/`0032` left it)
LIVESTOCK_NAMES: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    "cattle_young": (
        {
            "uz_latn": "Qoramol (2 yoshgacha, ona suti bilan oziqlanadigan buzoqlardan tashqari)",
            "uz_cyrl": "Қорамол (2 ёшгача, она сути билан озиқланадиган бузоқлардан ташқари)",
            "en": "Cattle, under 2 years (excluding suckling calves)",
        },
        {
            "uz_latn": "Qoramol (2 yoshgacha)",
            "uz_cyrl": "Қорамол (2 ёшгача)",
            "en": "Cattle, under 2 years",
        },
    ),
    "horse_young": (
        {
            "uz_latn": "Ot (2 yoshgacha, ona suti bilan oziqlanadigan toylardan tashqari)",
            "uz_cyrl": "От (2 ёшгача, она сути билан озиқланадиган тойлардан ташқари)",
            "en": "Horse, under 2 years (excluding suckling foals)",
        },
        {
            "uz_latn": "Ot (2 yoshgacha)",
            "uz_cyrl": "От (2 ёшгача)",
            "en": "Horse, under 2 years",
        },
    ),
    "camel_young": (
        {
            "uz_latn": "Tuya (2 yoshgacha, ona suti bilan oziqlanadigan boʻtaloqlardan tashqari)",
            "uz_cyrl": "Туя (2 ёшгача, она сути билан озиқланадиган бўталоқлардан ташқари)",
            "en": "Camel, under 2 years (excluding suckling calves)",
        },
        {
            "uz_latn": "Tuya (2 yoshgacha)",
            "uz_cyrl": "Туя (2 ёшгача)",
            "en": "Camel, under 2 years",
        },
    ),
    "donkey_young": (
        {
            "uz_latn": "Eshak (2 yoshgacha, ona suti bilan oziqlanadigan xoʻtiklardan tashqari)",
            "uz_cyrl": "Эшак (2 ёшгача, она сути билан озиқланадиган хўтиклардан ташқари)",
            "en": "Donkey, under 2 years (excluding suckling foals)",
        },
        {
            "uz_latn": "Eshak (2 yoshgacha)",
            "uz_cyrl": "Эшак (2 ёшгача)",
            "en": "Donkey, under 2 years",
        },
    ),
    "lamb_kid_under_6m": (
        {
            "uz_latn": "Qoʻzi va uloq (6 oygacha, ona suti bilan oziqlanadiganlaridan tashqari)",
            "uz_cyrl": "Қўзи ва улоқ (6 ойгача, она сути билан озиқланадиганларидан ташқари)",
            "en": "Lambs and kids, under 6 months (excluding suckling ones)",
        },
        {
            "uz_latn": "Qoʻzi va uloq (6 oygacha)",
            "uz_cyrl": "Қўзи ва улоқ (6 ойгача)",
            "en": "Lambs and kids, under 6 months",
        },
    ),
}


def _exactly_one(result: sa.CursorResult, what: str) -> None:
    if result.rowcount != 1:
        raise RuntimeError(
            f"expected exactly one row for {what}, updated {result.rowcount} — this "
            "database did not run the chain this revision depends on"
        )


def _set_orphanage(wording: dict) -> None:
    result = op.get_bind().execute(
        sa.text(
            "UPDATE classifier_items SET "
            "name = name || CAST(:name AS jsonb), "
            "props = props || jsonb_build_object('basis', CAST(:basis AS text)) "
            "WHERE code = :code AND status = 'active' AND classifier_id = "
            "(SELECT id FROM classifiers WHERE code = 'benefit_categories')"
        ).bindparams(
            name=json.dumps(wording["name"], ensure_ascii=False),
            basis=wording["basis"],
            code=ORPHANAGE_CODE,
        )
    )
    _exactly_one(result, f"benefit category {ORPHANAGE_CODE!r}")


def _set_livestock(code: str, name: dict[str, str]) -> None:
    result = op.get_bind().execute(
        sa.text(
            "UPDATE livestock_types SET name = name || CAST(:name AS jsonb), "
            "updated_at = now() WHERE code = :code"
        ).bindparams(name=json.dumps(name, ensure_ascii=False), code=code)
    )
    _exactly_one(result, f"livestock type {code!r}")


def upgrade() -> None:
    """Upgrade schema."""
    _set_orphanage(ORPHANAGE_NEW)
    for code, (new, _old) in LIVESTOCK_NAMES.items():
        _set_livestock(code, new)


def downgrade() -> None:
    """Downgrade schema."""
    for code, (_new, old) in LIVESTOCK_NAMES.items():
        _set_livestock(code, old)
    _set_orphanage(ORPHANAGE_OLD)
