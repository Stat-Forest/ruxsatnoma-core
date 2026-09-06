"""Permission codes owned by `archive`; importing registers them (same idiom
as `app/modules/norms/permissions.py`)."""

from app.modules.auth.permissions import register

ARCHIVE_MANAGE = "archive.manage"

register({ARCHIVE_MANAGE: "Archive a terminal application or permit, and read the register"})
