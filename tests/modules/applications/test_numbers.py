"""Tests for `app/core/numbers.py` — the public-number allocator (plan Task
5, ruling 5а: a row lock inside the caller's transaction, chosen over a
sequence specifically so a rolled-back submission leaves no gap).

`number_counters` is shared and persistent across every test run in this
worktree's test DB (lesson: "the test DB is shared, persistent, and never
empty"), so every scope below is a freshly randomised (prefix, year) pair —
never a fixed literal like "RX"/2027, which would accumulate across runs and
make any absolute-value assertion flaky from birth."""

import uuid
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.numbers import next_public_number


def _prefix() -> str:
    return f"T{uuid.uuid4().hex[:10].upper()}"


class _SimulatedFailure(Exception):
    """A deliberate signal, never a real error a `next_public_number` call
    could raise itself — so `pytest.raises` below cannot accidentally mask
    a genuine bug."""


async def test_first_number_in_a_fresh_scope_is_000001(db: AsyncSession) -> None:
    prefix = _prefix()
    number = await next_public_number(db, prefix, date(2031, 4, 1))
    assert number == f"{prefix}-2031-000001"


async def test_second_call_in_the_same_scope_continues_to_000002(db: AsyncSession) -> None:
    prefix = _prefix()
    on_date = date(2031, 6, 15)
    first = await next_public_number(db, prefix, on_date)
    second = await next_public_number(db, prefix, on_date)
    assert first == f"{prefix}-2031-000001"
    assert second == f"{prefix}-2031-000002"
    # A third call keeps going rather than repeating — continuity, not just "2".
    third = await next_public_number(db, prefix, on_date)
    assert third == f"{prefix}-2031-000003"


async def test_two_prefixes_keep_independent_counters(db: AsyncSession) -> None:
    on_date = date(2031, 3, 1)
    prefix_a, prefix_b = _prefix(), _prefix()
    a1 = await next_public_number(db, prefix_a, on_date)
    b1 = await next_public_number(db, prefix_b, on_date)
    a2 = await next_public_number(db, prefix_a, on_date)
    assert a1 == f"{prefix_a}-2031-000001"
    assert b1 == f"{prefix_b}-2031-000001"  # unaffected by prefix_a's own count
    assert a2 == f"{prefix_a}-2031-000002"


async def test_two_years_keep_independent_counters(db: AsyncSession) -> None:
    """Same prefix, different calendar year: a fresh scope either way, since
    `scope = f"{prefix}:{on_date.year}"`."""
    prefix = _prefix()
    first = await next_public_number(db, prefix, date(2031, 12, 31))
    second = await next_public_number(db, prefix, date(2032, 1, 1))
    assert first == f"{prefix}-2031-000001"
    assert second == f"{prefix}-2032-000001"


async def test_padding_is_exactly_six_digits(db: AsyncSession) -> None:
    prefix = _prefix()
    number = await next_public_number(db, prefix, date(2031, 1, 1))
    serial = number.rsplit("-", 1)[-1]
    assert serial == "000001"
    assert len(serial) == 6


async def test_a_rolled_back_allocation_is_reused(db: AsyncSession) -> None:
    """The whole reason for the row-lock design (ruling 5а) over a sequence:
    a submission that fails AFTER allocating its number leaves no gap — the
    next caller gets the exact number that was rolled back, not the one
    after it. This is the one property a sequence cannot provide.

    Exercises the real production shape (review M1): the counter row must
    already EXIST — `RX:2026` after the year's first successful submission —
    before the increment that gets rolled back. `first` is flushed for real
    (a genuine `UPDATE`, not merely a dirty Python attribute) so it survives
    below; the second allocation then runs inside a SAVEPOINT
    (`db.begin_nested()`, mirroring `signatures.service.sign()`'s own
    insert-race idiom — a bare `db.rollback()` here would undo `first`'s
    own flush too, not just the second increment) and is undone by letting
    an exception propagate out of it, never by calling `db.rollback()`
    directly inside the block.
    """
    prefix = _prefix()
    on_date = date(2031, 7, 1)

    first = await next_public_number(db, prefix, on_date)
    assert first == f"{prefix}-2031-000001"
    await db.flush()

    with pytest.raises(_SimulatedFailure):
        async with db.begin_nested():
            second = await next_public_number(db, prefix, on_date)
            assert second == f"{prefix}-2031-000002"
            raise _SimulatedFailure

    reused = await next_public_number(db, prefix, on_date)
    assert reused == f"{prefix}-2031-000002"
