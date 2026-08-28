"""0007 DDL: media_files, announcements, roles.status, ASCII pinfl, poa FK, seeds."""

import uuid
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.models import MediaFile
from app.modules.admin.models import Announcement, AnnouncementFile
from app.modules.auth.models import Role, RolePermission


async def make_file(db, *, uploaded_by=None, content_type="application/pdf") -> MediaFile:
    f = MediaFile(
        storage_key=f"t/{uuid.uuid4().hex}",
        filename="doc.pdf",
        content_type=content_type,
        size_bytes=100,
        sha256="0" * 64,
        uploaded_by=uploaded_by,
    )
    db.add(f)
    await db.flush()
    return f


async def test_media_file_defaults_and_status_check(db):
    f = await make_file(db)
    assert f.status == "active"
    f.status = "bogus"
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_media_file_storage_key_unique(db):
    f = await make_file(db)
    db.add(
        MediaFile(
            storage_key=f.storage_key,
            filename="x.pdf",
            content_type="application/pdf",
            size_bytes=1,
            sha256="1" * 64,
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_announcement_defaults_and_status_check(db):
    from tests.modules.auth.test_sessions import make_user

    author = await make_user(db)
    a = Announcement(title={"uz_cyrl": "Эълон"}, body={"uz_cyrl": "Матн"}, created_by=author.id)
    db.add(a)
    await db.flush()
    assert a.status == "draft"
    a.status = "bogus"
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_announcement_file_link(db):
    from tests.modules.auth.test_sessions import make_user

    author = await make_user(db)
    a = Announcement(title={"uz_cyrl": "Эълон"}, body={"uz_cyrl": "Матн"}, created_by=author.id)
    f = await make_file(db)
    db.add(a)
    await db.flush()
    db.add(AnnouncementFile(announcement_id=a.id, file_id=f.id))
    await db.flush()
    link = (
        await db.execute(select(AnnouncementFile).where(AnnouncementFile.announcement_id == a.id))
    ).scalar_one()
    assert link.file_id == f.id and link.position == 0


async def test_role_status_check_and_default(db):
    role = Role(code=f"r-{uuid.uuid4().hex[:8]}", name={"uz_cyrl": "Роль"})
    db.add(role)
    await db.flush()
    assert role.status == "active"
    role.status = "bogus"
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_users_pinfl_check_is_ascii_only(db):
    """3.2b carry-over: '\\d' in PG is not Unicode, but the old constraint text was;
    the swapped CHECK must reject non-ASCII digit strings at the DB level."""
    from tests.modules.auth.test_sessions import make_user

    user = await make_user(db)
    user.pinfl = "١٢٣٤٥٦٧٨٩٠١٢٣٤"  # 14 Arabic-Indic digits
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_poa_file_fk_enforced(db):
    """representations.poa_file_id now points at media_files."""
    from app.modules.auth.models import Applicant, Representation
    from tests.modules.auth.test_sessions import make_user

    user = await make_user(db, role_code="applicant")
    applicant = Applicant(kind="legal", stir=str(uuid.uuid4().int)[:9], name="ООО Тест")
    db.add(applicant)
    await db.flush()
    db.add(
        Representation(
            applicant_id=applicant.id,
            user_id=user.id,
            basis="poa",
            poa_file_id=uuid.uuid4(),  # no such file
            valid_from=date(2026, 1, 1),
            valid_until=date(2026, 12, 31),
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_users_view_seeded_to_three_roles(db):
    """Subset, not exact-equality (Task 6, С23): `PUT /admin/roles/{id}/permissions`
    gives other tests a legitimate way to grant auth.users.view to further roles in
    this shared/persistent table — migration 0007's own three seeded grants must
    still be present, but they need not be the only ones anymore."""
    rows = (
        await db.execute(
            select(Role.code)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .where(RolePermission.permission_code == "auth.users.view")
        )
    ).scalars()
    assert {"central_admin", "leadership", "executor_head"} <= set(rows)


async def test_permission_codes_registered():
    from app.modules.admin import permissions as admin_permissions
    from app.modules.auth.permissions import PERMISSIONS

    assert "auth.users.view" in PERMISSIONS
    assert admin_permissions.ANNOUNCEMENTS_MANAGE in PERMISSIONS
