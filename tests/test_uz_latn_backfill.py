"""`migrations/versions/0032_uz_latn_backfill.py`'s transliteration and the
recursive JSONB walker around it (decision #90).

Loaded via `importlib` rather than a normal import: no migration script imports
from `app`, or from another migration, on purpose (`0032`'s own docstring) — a
migration is a frozen record of what it did on the day it was written, and this
test exists precisely to prove that record correct without creating a path for
something outside it to ever change what it would produce on replay. A normal
`import migrations.versions...` would need `migrations/versions/` to be a real
package (it deliberately is not one — alembic loads scripts by path, not by
`import`), so the loader below is the plain-stdlib equivalent of what alembic
itself does.
"""

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_migration_0032() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0032_uz_latn_backfill.py"
    )
    spec = importlib.util.spec_from_file_location("_migration_0032_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_m = _load_migration_0032()
translit = _m.translit_uz_cyrl_to_latn

# The 15 gis_layers pairs: uz_cyrl as `0010_gis.py::LAYER_SEED` seeded it, uz_latn
# as `0031_gis_layer_uz_latn.py::UZ_LATN_NAMES` already carries it, hand-verified
# and merged — known-correct input/output pairs already sitting on this branch,
# so no output here is asserted from a fresh guess.
LAYER_PAIRS: list[tuple[str, str]] = [
    ("Ўрмон фонди чегаралари", "Oʻrmon fondi chegaralari"),
    ("Ташкилот чегаралари", "Tashkilot chegaralari"),
    ("Контурлар", "Konturlar"),
    ("Яйловлар", "Yaylovlar"),
    ("Пичанзорлар", "Pichanzorlar"),
    ("Асаларичилик жойлари", "Asalarichilik joylari"),
    ("Рекреация ҳудудлари", "Rekreatsiya hududlari"),
    ("Чекловлар", "Cheklovlar"),
    ("Муҳофаза зоналари", "Muhofaza zonalari"),
    ("Ротация", "Rotatsiya"),
    ("Дам бериш тақвими", "Dam berish taqvimi"),
    ("Сув нуқталари", "Suv nuqtalari"),
    ("Чорва йўлаклари", "Chorva yoʻlaklari"),
    ("Ёнғин тақиқлари", "Yongʻin taqiqlari"),
    ("Махсус ажратилган майдонлар", "Maxsus ajratilgan maydonlar"),
]


def test_translit_matches_every_known_gis_layer_pair():
    for uz_cyrl, expected_uz_latn in LAYER_PAIRS:
        assert translit(uz_cyrl) == expected_uz_latn


def test_translit_handles_the_letters_the_layer_names_never_exercise():
    # э is a different letter from е and is always "e" — never the word-initial
    # "ye" rule (real word: "endi", now).
    assert translit("Энди") == "Endi"
    # ъ (tutuq belgisi) -> U+02BC, distinct from oʻ/gʻ's U+02BB (real word:
    # "maʼno", meaning).
    assert translit("маъно") == "maʼno"
    # е is "ye" at the start of a word, "e" elsewhere (real words: "yer", place;
    # "tepa", hill).
    assert translit("ер") == "yer"
    assert translit("тепа") == "tepa"
    # ё is always "yo", regardless of position (real word: "tayyor", ready —
    # unlike Russian, Uzbek е/ё/ю/я never depend on position except е itself).
    assert translit("тайёр") == "tayyor"


def test_translit_preserves_case_including_all_caps_digraph_runs():
    assert translit("чорва") == "chorva"
    assert translit("Чорва") == "Chorva"
    # A digraph as the LAST letter of an all-caps run must still read as caps —
    # the rule looks at the neighbour on EITHER side, not just the next letter,
    # or "БОШ" would come out "BOSh" instead of "BOSH" (real word: "bosh", head).
    assert translit("БОШ") == "BOSH"
    assert translit("Чорва".upper()) == "CHORVA"


def test_translit_passes_through_whatever_it_does_not_recognise():
    # Latin text, digits and punctuation inside a real seeded string
    # ("Тайёрланган пичан ҳажми (тонна/м3)" from migration 0026) must survive
    # untouched.
    assert translit("тонна/м3 (2026)") == "tonna/m3 (2026)"
    assert translit("") == ""


def test_translit_is_a_pure_function():
    text = "Ёнғин тақиқлари"
    before = text
    assert translit(text) == translit(text)
    assert text == before  # no mutation of the input


def test_backfill_value_adds_uz_latn_only_where_missing_or_blank():
    backfill = _m._backfill_value

    present, changed = backfill({"uz_cyrl": "Контурлар", "uz_latn": "Already there"})
    assert not changed
    assert present["uz_latn"] == "Already there"

    blank, changed = backfill({"uz_cyrl": "Контурлар", "uz_latn": "  "})
    assert changed
    assert blank["uz_latn"] == "Konturlar"

    absent, changed = backfill({"uz_cyrl": "Контурлар", "ru": "Контуры"})
    assert changed
    assert absent == {"uz_cyrl": "Контурлар", "ru": "Контуры", "uz_latn": "Konturlar"}


def test_backfill_value_recurses_into_lists_and_leaves_the_rest_alone():
    backfill = _m._backfill_value

    items = [
        {"code": "a", "question": {"uz_cyrl": "Контурлар"}, "required": True},
        {"code": "b", "question": {"uz_cyrl": "Яйловлар", "uz_latn": "Custom"}},
    ]
    new_items, changed = backfill(items)
    assert changed
    assert new_items[0]["question"]["uz_latn"] == "Konturlar"
    assert new_items[1]["question"]["uz_latn"] == "Custom"  # untouched — not blank
    assert new_items[0]["code"] == "a" and new_items[0]["required"] is True

    unrelated, changed = backfill({"account": "12345", "bank": "MB"})
    assert not changed
    assert unrelated == {"account": "12345", "bank": "MB"}


def test_revert_value_only_removes_what_it_can_still_derive():
    revert = _m._revert_value

    ours, changed = revert({"uz_cyrl": "Контурлар", "uz_latn": "Konturlar"})
    assert changed
    assert "uz_latn" not in ours

    edited, changed = revert({"uz_cyrl": "Контурлар", "uz_latn": "Qo'rg'onlar"})
    assert not changed
    assert edited["uz_latn"] == "Qo'rg'onlar"
