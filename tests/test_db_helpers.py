"""Unit tests for shared DB helpers in app/db.py."""

import uuid

from app.db import uuid7


def test_uuid7_is_stdlib_uuid_version_7():
    u = uuid7()
    assert isinstance(u, uuid.UUID)
    assert u.version == 7


def test_uuid7_is_monotonic():
    ids = [uuid7() for _ in range(1000)]
    assert ids == sorted(ids)
