"""The enum-ish columns' one source of truth (lesson): the tuple in
`models.py`, mirrored by hand in `schemas.py`'s `Literal`s and in the
migration's CHECK constraints. This test pins the two Python sides equal;
the migration itself is pinned by `tests/test_migrations.py`'s
autogenerate-diff guard. `schemas.py` and `models.py` both reference this
file by name already — it did not exist until this track added it."""

from app.modules.search.models import EXPORT_FORMATS, EXPORT_STATUSES, SEARCH_KINDS
from app.modules.search.schemas import ExportFormat, ExportStatus, SearchKind


def test_search_kind_literal_matches_the_check_constraint():
    assert set(SearchKind.__args__) == set(SEARCH_KINDS)


def test_export_format_literal_matches_the_check_constraint():
    assert set(ExportFormat.__args__) == set(EXPORT_FORMATS)


def test_export_status_literal_matches_the_check_constraint():
    assert set(ExportStatus.__args__) == set(EXPORT_STATUSES)
