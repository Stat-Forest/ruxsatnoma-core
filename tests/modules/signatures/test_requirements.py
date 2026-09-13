"""The requirement set (`required_purposes`/`is_complete`/`missing_purposes`/
`require_complete`) -- the public surface 3.9 and 3.11 build against."""

import secrets
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.models import SystemSetting
from app.core.settings_store import invalidate
from app.modules.auth.models import User
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.signatures import service
from tests.modules.auth.test_sessions import make_user


def _pinfl() -> str:
    """A fresh, valid-shape (`^[0-9]{14}$`) pinfl per call -- `users.pinfl` is
    UNIQUE, and this fixture may run many times against the same shared test
    database (same reasoning as test_sign.py's own `_pinfl`)."""
    return f"{secrets.randbelow(10**14):014d}"


@pytest.fixture
async def a_user(db: AsyncSession) -> User:
    return await make_user(db, pinfl=_pinfl())


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """`get_setting` caches per process for 60 seconds (settings_store.py's
    own docstring). Mirrors tests/core/test_settings_store.py's own autouse
    fixture: clear before AND after every test here, so neither a stale
    default cached by an earlier test nor the override this file writes in
    `test_changing_the_setting_changes_the_requirement_with_no_code_change`
    can leak into whatever runs next in this process."""
    invalidate()
    yield
    invalidate()


async def _override(db, value: str) -> None:
    """Write a settings override the way `admin` does, then drop the 60-second
    per-process cache -- without the invalidate the service reads a stale
    value (pre-flight ruling P2)."""
    await db.merge(SystemSetting(key="permit_required_signatures", value=value))
    await db.flush()
    invalidate("permit_required_signatures")


DOC = b"permit"


async def test_the_default_permit_requirement_is_the_three_leshoz_lines(db):
    assert await service.required_purposes(db, "permit") == [
        "permit_head",
        "permit_chief_forester",
        "permit_accountant",
    ]


async def test_required_purposes_is_empty_for_an_unknown_object_type(db):
    # "application" has no configured requirement today -- not an error, just
    # nothing to require (3.9 may register its own settings row later).
    assert await service.required_purposes(db, "application") == []


async def test_is_complete_and_require_complete_agree_on_an_empty_requirement_set(db):
    # No object type nobody requires a signature on ever raises -- `is_complete`
    # and `require_complete` must agree, not just happen to both pass today.
    obj = uuid.uuid4()
    assert await service.is_complete(db, object_type="application", object_id=obj) is True
    await service.require_complete(db, object_type="application", object_id=obj)  # no raise


async def test_an_incomplete_set_names_exactly_what_is_missing(db, a_user):
    obj = uuid.uuid4()
    await service.sign(
        db,
        object_type="permit",
        object_id=obj,
        purpose="permit_head",
        document=DOC,
        pkcs7=encode_mock_signature(
            document=DOC, serial="SER-1", issuer="ISS-1", pinfl=a_user.pinfl
        ),
        user=a_user,
    )
    assert await service.is_complete(db, object_type="permit", object_id=obj) is False
    # Direct call, not just the exception below: a "public surface" task's
    # own test must exercise the NEW function itself, not only the error
    # path a different function happens to raise through it
    # (.claude/lessons.md).
    assert await service.missing_purposes(db, object_type="permit", object_id=obj) == [
        "permit_chief_forester",
        "permit_accountant",
    ]
    with pytest.raises(DomainError) as exc:
        await service.require_complete(db, object_type="permit", object_id=obj)
    assert exc.value.code == "ERR-SIGN-003"
    assert exc.value.details is not None
    assert exc.value.details["missing"] == [
        "permit_chief_forester",
        "permit_accountant",
    ]


async def test_changing_the_setting_changes_the_requirement_with_no_code_change(db, a_user):
    # ruling 7: the Agency's unanswered question is one settings row, not an `if`
    obj = uuid.uuid4()
    await _override(db, "permit_head")
    await service.sign(
        db,
        object_type="permit",
        object_id=obj,
        purpose="permit_head",
        document=DOC,
        pkcs7=encode_mock_signature(
            document=DOC, serial="SER-1", issuer="ISS-1", pinfl=a_user.pinfl
        ),
        user=a_user,
    )
    assert await service.is_complete(db, object_type="permit", object_id=obj) is True
    await service.require_complete(db, object_type="permit", object_id=obj)  # no raise


async def test_required_purposes_parsing_tolerates_whitespace_and_a_trailing_comma(db):
    # Task 6 (deferred minor): the comma-split handled these cases correctly
    # by inspection, but only the clean default value had a test until now.
    await _override(db, " permit_head , permit_chief_forester ,")
    assert await service.required_purposes(db, "permit") == [
        "permit_head",
        "permit_chief_forester",
    ]


async def test_an_empty_override_is_rejected_and_falls_back_to_the_default(db):
    """The edge Task 6 flagged as untested, checked directly rather than by
    inspection: `settings_store.coerce` rejects an empty (or whitespace-
    only) string as malformed ("expected text"), so `get_setting` logs
    `system_setting_invalid` and falls back to the code DEFAULT rather than
    letting `""` through -- `permit_required_signatures` can never actually
    be switched off via an empty override the way one might expect; the
    real three-line requirement (ruling #210) stays in force. The only way `required_purposes`
    returns `[]` is an object_type with NO entry in `_REQUIREMENT_SETTINGS`
    at all (`"application"`, above) -- never an override of this key."""
    await _override(db, "")
    assert await service.required_purposes(db, "permit") == [
        "permit_head",
        "permit_chief_forester",
        "permit_accountant",
    ]


async def test_an_invalid_signature_never_satisfies_a_requirement(db, a_user):
    """RI-05 territory: a bad pkcs7 is stored as evidence (ruling 8) but must
    never count toward completeness -- only `verification_status == "valid"`
    does. Deliberately uses the DEFAULT requirement set, not `_override`:
    `sign()`'s own refusal path commits EVERYTHING pending on `db` (its
    documented transaction contract), so an override written earlier in this
    same test would be committed for real by the expected refusal below --
    exactly the shared-DB poisoning `.claude/lessons.md` warns about."""
    obj = uuid.uuid4()
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=obj,
            purpose="permit_head",
            document=DOC,
            pkcs7=encode_mock_signature(
                document=b"different-bytes", serial="SER-2", issuer="ISS-2", pinfl=a_user.pinfl
            ),
            user=a_user,
        )
    assert exc.value.code == "ERR-SIGN-001"
    assert await service.is_complete(db, object_type="permit", object_id=obj) is False
    assert "permit_head" in await service.missing_purposes(db, object_type="permit", object_id=obj)
