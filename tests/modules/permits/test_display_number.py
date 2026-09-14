"""The permit's printed number has ONE spelling — «А №000004»: the series, a
space, then № glued to six digits (Oybek, 2026-09-14: no space after №). The
document, every notification, the exports and the search index all print it,
and a result list spelling the same identifier a second way is how a person
decides they found a different permit — so the Python formatter and its SQL
twin are asserted against each other on a real row, not trusted to agree."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.permits import service
from app.modules.permits.models import Permit, display_number, display_number_sql

pytestmark = pytest.mark.asyncio


def test_no_space_between_the_sign_and_the_digits() -> None:
    assert display_number("А", 4) == "А №000004"
    assert display_number("А", 123456) == "А №123456"


def test_the_service_formatter_is_the_same_function() -> None:
    assert service._permit_number("Б", 7) == display_number("Б", 7) == "Б №000007"


async def test_the_sql_twin_prints_exactly_what_python_prints(
    db: AsyncSession, issued_permit: Permit
) -> None:
    printed_by_sql = (
        await db.execute(select(display_number_sql()).where(Permit.id == issued_permit.id))
    ).scalar_one()
    assert printed_by_sql == display_number(issued_permit.series, issued_permit.number)
