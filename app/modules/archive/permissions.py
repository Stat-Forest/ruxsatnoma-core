"""Permission codes owned by `archive`; importing registers them (same idiom
as `app/modules/norms/permissions.py`).

Split into two codes (F23, `docs/plans/07.3-findings.md`): the original single
`archive.manage` bundled a READ (the register and one item) with a WRITE
(archiving, verifying) under one code, so decision #95 — every read reaches
the prosecutor, no write ever does — could not be honoured: granting the code
to the prosecutor would also let it archive things. `archive.view` is the read
half; `archive.manage` keeps only the write half."""

from app.modules.auth.permissions import register

ARCHIVE_VIEW = "archive.view"
ARCHIVE_MANAGE = "archive.manage"

register(
    {
        ARCHIVE_VIEW: "Read the archive register and one archived item",
        ARCHIVE_MANAGE: "Archive a terminal application or permit, or verify a stored item",
    }
)
