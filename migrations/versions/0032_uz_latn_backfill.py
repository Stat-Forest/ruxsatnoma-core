"""Backfill uz_latn from uz_cyrl across every LocalizedName-shaped JSONB column.

Decision #90: `LocalizedName` (`app/core/schemas.py`) is about to start requiring
`uz_latn` instead of `uz_cyrl`. Flipping the validator alone would reject every row
written before today — `uz_cyrl` has been the required language since `0010`, so
every existing row has it and none is guaranteed to have `uz_latn`. This migration
is the backfill that has to land first; the validator flip itself is a plain-code
change in the same commit, not part of this file.

Migration `0031` already did this by hand for the 15 `gis_layers` rows (a fixed,
never-admin-edited catalogue, so a hardcoded `code -> uz_latn` dict was the right
tool). Everywhere else, `uz_cyrl` is free text an operator typed, so there is no
fixed dict to hardcode — this migration instead carries a general Cyrillic ->
Latin transliteration function and runs it over whatever each table actually
holds. `gis_layers` itself is deliberately NOT touched again here.

**Where the seventeen-ish columns are** (found by grepping `JSONB` in
`app/modules/*/models.py` and `LocalizedName` in `app/modules/*/schemas.py`,
against the twelve migrations that seed `uz_cyrl` data: `0003`, `0005`, `0009`,
`0010`, `0018`-`0020`, `0022`-`0026`), across eight modules:

- `admin`: `regions.name`, `districts.name`, `organizations.name`,
  `classifiers.name`, `classifier_items.name`, `activity_types.name`,
  `livestock_types.name`, `announcements.title`, `announcements.body`.
- `auth`: `roles.name`, `roles.description`.
- `help`: `faq_items.question`, `faq_items.answer`.
- `inspections`: `checklists.name`, and `checklists.items` — a JSONB ARRAY whose
  elements each carry their own `question` LocalizedName (`ChecklistQuestion` in
  `inspections/schemas.py`), not a flat column.
- `reports`: `report_forms.name`, and `report_forms.columns` — same shape as
  `checklists.items`, one `label` LocalizedName per column (`ReportFormColumn`).
- `gis`: `layer_features.name` (nullable — an admin-drawn feature may have none).
- `notifications`: `notification_templates.subject` (nullable, email only) and
  `.body`.
- `permits`: `permit_templates.name` — seeded by `0019`, read generically as
  `dict[str, Any]`; this module never imported `LocalizedName` itself (nothing
  exposes a template-editing API yet), but the JSONB shape is the same and the
  flip would reject a template update through any future admin screen just the
  same, so it gets the same backfill.

Two columns carry the array shape; every other one is a flat LocalizedName dict.
Both shapes are handled by ONE recursive walker (`_backfill_value`) rather than
two copies of the same logic, because a column can be added to `TARGETS` without
anyone having to know in advance whether the day's data happens to be nested.

**The backfill is deterministic** (decision #90): `uz_cyrl` is walked through
`translit_uz_cyrl_to_latn` and the result becomes `uz_latn` wherever that key is
absent or blank. Nothing is ever derived from `ru` — a Russian name pushed through
a Cyrillic-to-Latin table is not an Uzbek name, and would be a worse placeholder
than the Cyrillic it replaced.

**The transliteration function lives here, not in `app/core`.** No migration
before this one imports from `app` — each is a frozen record of what it did to
the schema and data on the day it was written, independent of code that keeps
changing underneath it. A `translit_uz_cyrl_to_latn` under `app/core` would break
that: fixing a transliteration bug later would silently change what THIS
migration produces when replayed on a fresh database, years after the fact,
which is exactly the failure `lessons.md`'s "never edit an applied migration in
place" rule exists to prevent, one import away. A future admin-facing "suggest a
Latin spelling" feature needs its own copy in `app/core`, not an import of this
one.

**The downgrade is honest about what it cannot know.** It removes a `uz_latn` key
only when it still equals what `translit_uz_cyrl_to_latn(uz_cyrl)` produces right
now — the same test the upgrade used to decide whether to write it. That
distinguishes "untouched since this migration wrote it" from "an operator has
since corrected it" in the common case, cheaply, without a shadow column — but it
is a heuristic, not a certainty: an operator who edited `uz_cyrl` afterward in a
way that happens to transliterate to the very `uz_latn` they also typed would
have that value stripped on downgrade too. Nothing here can fully tell "ours"
from "a coincidence"; this is the cheap 90% rather than a tracking column for the
remaining 10%.
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Uzbek Cyrillic -> Uzbek Latin, letter by letter. A well-defined, well-known
# mapping (decision #90) — not a guess, and not the same table 0031 used (that
# one was 15 whole strings, hand-verified against the seed; this is the general
# rule those 15 pairs happen to satisfy, which is exactly what the tests check).
# ---------------------------------------------------------------------------

# Letters whose Latin form is exactly one character.
_SINGLE: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ж": "j", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x",
    "э": "e", "қ": "q", "ҳ": "h",
    # Not part of the official Uzbek Cyrillic alphabet, but a Russian-origin
    # proper noun occasionally carries one: a defensive fallback rather than
    # leaving the letter untranslated. "ь" has no Latin sound of its own.
    "ы": "i", "ь": "",
}  # fmt: skip

# Letters whose Latin form is two characters — a digraph, or a base letter plus
# the U+02BB modifier (matching 0031, not an ASCII apostrophe).
_WIDE: dict[str, str] = {
    "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh",
    "ё": "yo", "ю": "yu", "я": "ya",
    "ў": "oʻ", "ғ": "gʻ",
}  # fmt: skip

# ъ (tutuq belgisi) is its own case: one character, but a DIFFERENT modifier
# letter than oʻ/gʻ's — U+02BC, not U+02BB.
_TUTUQ = "ʼ"

_MAPPED = frozenset(_SINGLE) | frozenset(_WIDE) | {"ъ", "е"}


def translit_uz_cyrl_to_latn(text: str) -> str:
    """Uzbek Cyrillic -> Uzbek Latin. Pure, and total over any input string.

    Every officially-Uzbek letter maps one-to-one except three: `ъ` becomes the
    U+02BC modifier (never an ASCII apostrophe); `е` becomes "ye" at the start of
    a word and plain "e" elsewhere ("Endi" from "Энди" is unaffected — that is a
    different letter, `э`, which is always "e"); and any digraph or letter+modifier
    output (ts/ch/sh/yo/yu/ya, oʻ/gʻ) takes title case ("Ch") on a capital that
    sits alone, and full caps ("CH") on one inside an all-caps run — decided by
    looking at the immediate neighbours, since nothing else marks where a caps
    run starts or ends. A character this table does not recognise (Latin text,
    digits, punctuation, the fifth locale's own script) passes through unchanged.
    """
    out: list[str] = []
    n = len(text)
    for i, ch in enumerate(text):
        lower = ch.lower()
        if lower not in _MAPPED:
            out.append(ch)
            continue
        prev_alpha = i > 0 and text[i - 1].isalpha()
        if lower == "е":
            latin = "e" if prev_alpha else "ye"
        elif lower == "ъ":
            latin = _TUTUQ
        elif lower in _WIDE:
            latin = _WIDE[lower]
        else:
            latin = _SINGLE[lower]
        if not latin or not ch.isupper():
            out.append(latin)
            continue
        if len(latin) == 1:
            out.append(latin.upper())
            continue
        prev_upper = prev_alpha and text[i - 1].isupper()
        next_upper = i + 1 < n and text[i + 1].isalpha() and text[i + 1].isupper()
        out.append(latin.upper() if (prev_upper or next_upper) else latin[0].upper() + latin[1:])
    return "".join(out)


# ---------------------------------------------------------------------------
# (table, column) pairs to backfill. `gis_layers` is deliberately absent — 0031
# already gave it real, hand-checked names; running a generic transliteration
# over it again would be redundant at best.
# ---------------------------------------------------------------------------
TARGETS: list[tuple[str, str]] = [
    ("regions", "name"),
    ("districts", "name"),
    ("organizations", "name"),
    ("classifiers", "name"),
    ("classifier_items", "name"),
    ("activity_types", "name"),
    ("livestock_types", "name"),
    ("announcements", "title"),
    ("announcements", "body"),
    ("roles", "name"),
    ("roles", "description"),
    ("faq_items", "question"),
    ("faq_items", "answer"),
    ("checklists", "name"),
    ("checklists", "items"),
    ("report_forms", "name"),
    ("report_forms", "columns"),
    ("layer_features", "name"),
    ("notification_templates", "subject"),
    ("notification_templates", "body"),
    ("permit_templates", "name"),
]


def _is_localized_dict(value: Any) -> bool:
    cyrl = value.get("uz_cyrl") if isinstance(value, dict) else None
    return isinstance(cyrl, str) and cyrl.strip() != ""


def _has_uz_latn(value: dict[str, Any]) -> bool:
    latn = value.get("uz_latn")
    return isinstance(latn, str) and latn.strip() != ""


def _backfill_value(value: Any) -> tuple[Any, bool]:
    """Recursively add `uz_latn` wherever a dict looks LocalizedName-shaped (a
    non-blank string `uz_cyrl`) and has none yet — a flat name, or one nested any
    number of levels inside a list/dict (`checklists.items[*].question`). Returns
    a new structure and whether anything changed; never mutates its argument."""
    if isinstance(value, dict):
        changed_here = False
        result = dict(value)
        if _is_localized_dict(result) and not _has_uz_latn(result):
            result["uz_latn"] = translit_uz_cyrl_to_latn(result["uz_cyrl"])
            changed_here = True
        any_child_changed = False
        for key, child in result.items():
            new_child, child_changed = _backfill_value(child)
            if child_changed:
                result[key] = new_child
                any_child_changed = True
        return result, (changed_here or any_child_changed)
    if isinstance(value, list):
        new_list = list(value)
        any_changed = False
        for idx, item in enumerate(value):
            new_item, changed = _backfill_value(item)
            if changed:
                new_list[idx] = new_item
                any_changed = True
        return new_list, any_changed
    return value, False


def _revert_value(value: Any) -> tuple[Any, bool]:
    """The downgrade's mirror of `_backfill_value`: strip a `uz_latn` this
    migration is likely to have written (see the module docstring's honesty
    note about the heuristic), recursing the same way."""
    if isinstance(value, dict):
        changed_here = False
        result = dict(value)
        cyrl = result.get("uz_cyrl")
        latn = result.get("uz_latn")
        if (
            isinstance(cyrl, str)
            and cyrl.strip()
            and isinstance(latn, str)
            and latn == translit_uz_cyrl_to_latn(cyrl)
        ):
            del result["uz_latn"]
            changed_here = True
        any_child_changed = False
        for key, child in list(result.items()):
            new_child, child_changed = _revert_value(child)
            if child_changed:
                result[key] = new_child
                any_child_changed = True
        return result, (changed_here or any_child_changed)
    if isinstance(value, list):
        new_list = list(value)
        any_changed = False
        for idx, item in enumerate(value):
            new_item, changed = _revert_value(item)
            if changed:
                new_list[idx] = new_item
                any_changed = True
        return new_list, any_changed
    return value, False


def _apply(walker: Any) -> None:
    bind = op.get_bind()
    for table, column in TARGETS:
        # table/column names come only from the hardcoded TARGETS list above,
        # never from data — not a SQL-injection surface despite the f-string.
        rows = bind.execute(
            sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")
        ).fetchall()
        for row_id, raw_value in rows:
            new_value, changed = walker(raw_value)
            if not changed:
                continue
            bind.execute(
                sa.text(
                    f"UPDATE {table} SET {column} = CAST(:value AS jsonb) "
                    f"WHERE id = CAST(:row_id AS uuid)"
                ).bindparams(value=json.dumps(new_value), row_id=str(row_id))
            )


def upgrade() -> None:
    _apply(_backfill_value)


def downgrade() -> None:
    _apply(_revert_value)
