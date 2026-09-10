"""С12's public check, plus the permit's map contour — Task 5, behind ruling R2's
flag (`public_permit_contour_enabled`).

`active_permit`, not `issued_permit`: the raw `issued_permit` fixture sits in
`pending_signatures`, one of `service.NON_PUBLIC_STATUSES` — the public check
would answer `{"found": False}` for it regardless of the contour flag, which
would test nothing about the contour at all. `active_permit` (this conftest,
`tests/modules/permits/conftest.py`) is the one that reached `active` through
the four real signatures, matching every "found: true" test in
`test_public_check.py`.

`set_setting` takes no `updated_by` and does not invalidate the process cache
itself (`app/core/settings_store.py`'s own docstring) — the same pattern
`tests/modules/public/test_site_settings.py::test_an_edited_contact_is_served`
uses: commit, then `settings_store.invalidate(key)`, and restore the row (this
key has no default row in the seed data, so "restore" means deleting the
override) in a `finally` block, because this test database is shared and
persistent across runs.
"""

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.models import SystemSetting
from app.core.settings_store import set_setting
from app.modules.permits.models import Permit

CHECK = "/api/v1/public/permits/check"
_KEY = "public_permit_contour_enabled"


async def _enable_contour(db: AsyncSession) -> None:
    await set_setting(db, _KEY, True)
    await db.commit()
    settings_store.invalidate(_KEY)


async def _restore_default(db: AsyncSession) -> None:
    await db.execute(delete(SystemSetting).where(SystemSetting.key == _KEY))
    await db.commit()
    settings_store.invalidate(_KEY)


async def test_the_contour_is_absent_by_default(client, active_permit: Permit) -> None:
    """Ruling R2: personal geodata stays off until the Agency confirms in
    writing — a fresh test database never overrides this key, so the default
    from `SETTING_SPECS` is what answers."""
    body = (
        await client.get(
            CHECK, params={"series": active_permit.series, "number": active_permit.number}
        )
    ).json()
    assert body["found"] is True
    assert body.get("contour") is None


async def test_the_contour_appears_when_the_flag_is_on(
    client, db: AsyncSession, active_permit: Permit
) -> None:
    try:
        await _enable_contour(db)
        body = (
            await client.get(
                CHECK, params={"series": active_permit.series, "number": active_permit.number}
            )
        ).json()
        assert body["found"] is True
        assert body["contour"]["type"] in {"Polygon", "MultiPolygon"}
        assert body["contour"]["coordinates"]
    finally:
        await _restore_default(db)


async def test_a_miss_still_answers_only_found_false(client, db: AsyncSession) -> None:
    """The flag being on must not turn a MISS into a differently-shaped
    response — `{"found": false}` stays the whole body, contour or not."""
    try:
        await _enable_contour(db)
        body = (await client.get(CHECK, params={"series": "А", "number": 999999999})).json()
        assert body == {"found": False}
    finally:
        await _restore_default(db)
