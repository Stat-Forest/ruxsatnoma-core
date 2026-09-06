"""The enum-ish columns' one source of truth (lesson): the tuple in
`models.py`, mirrored by hand in `schemas.py`'s `Literal`s and in the
migration's CHECK constraint. This test pins the first two equal; the
migration itself is pinned by `tests/test_migrations.py`'s autogenerate-diff
guard."""

from app.modules.oversight.models import (
    RI_LEVEL_BY_CODE,
    RISK_INDICATOR_CODES,
    RISK_INDICATOR_LEVELS,
    RISK_INDICATOR_STATUSES,
)
from app.modules.oversight.schemas import (
    RiskIndicatorCode,
    RiskIndicatorLevel,
    RiskIndicatorStatus,
)


def test_schema_literal_matches_the_code_tuple():
    assert set(RiskIndicatorCode.__args__) == set(RISK_INDICATOR_CODES)


def test_schema_literal_matches_the_level_tuple():
    assert set(RiskIndicatorLevel.__args__) == set(RISK_INDICATOR_LEVELS)


def test_schema_literal_matches_the_status_tuple():
    assert set(RiskIndicatorStatus.__args__) == set(RISK_INDICATOR_STATUSES)


def test_every_code_has_exactly_one_severity():
    assert set(RI_LEVEL_BY_CODE) == set(RISK_INDICATOR_CODES)
    assert set(RI_LEVEL_BY_CODE.values()) <= set(RISK_INDICATOR_LEVELS)
