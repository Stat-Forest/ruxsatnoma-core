"""Search — a level-5 reader (design/01 rule 5, design/02 § search).

Two tables belong to this module: `saved_filters` (a user's own saved search
profile, optionally shared with roles/users) and `export_jobs` (С22, decision
#98 — the prosecutor's watermarked PDF/XLSX export of a search result set,
built beside `saved_filters` exactly where `design/02` always put it). The
actual search itself reads `applications`/`permits`/`applicants` directly
through `search/repo.py` (the reader exception) and writes nothing there —
see `04.5-4.7-search-archive.md` ruling 1 for why this module carries no
tsvector column of its own on those tables (it would have to be declared in
THEIR `models.py`, which a reader may never edit).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

# The two `kind`s `search/repo.py` knows how to query today (plan ruling 2).
# One source of truth: this tuple builds both the CHECK below and
# `schemas.SearchKind` — see `test_models.py`'s literal-matches-check guard,
# the same shape `norms.models.LIVESTOCK_GROUPS` uses.
SEARCH_KINDS = ("applications", "permits")


class SavedFilter(Base):
    """A saved search profile (design/02 § search: `saved_filters`).

    `params` is whatever `kind`'s query parameters the user wants to replay —
    opaque JSONB, validated only at USE time (against the current `SearchQuery`
    schema), never at save time: a profile saved before a filter was added or
    removed must still load, just with that key ignored or absent.

    `shared` is `None` for a private profile, or
    `{"role_codes": [...], "user_ids": [...]}` for one visible to other users
    too — the same shape `admin.models.Announcement.audience` already uses for
    "a missing key or a null column means no restriction on that axis", except
    here the column itself being `None` means "shared with nobody but the
    owner" rather than "everybody".
    """

    __tablename__ = "saved_filters"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str]
    kind: Mapped[str]
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    shared: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(f"kind IN {SEARCH_KINDS}", name="kind_valid"),
        UniqueConstraint("user_id", "name", name="uq_saved_filters_user_name"),
    )


# `design/02` names this table for an ASYNCHRONOUS export worker (`status`
# queued/processing/done/failed). This implementation is deliberately
# SYNCHRONOUS instead (plan header, С22 track brief): a 10 000-row cap
# renders in well under a request timeout, exactly the same call
# `reports/render.py` already makes for its own PDF/XLSX export, and a real
# queue would need a worker, a poll route and a retry policy for a feature
# nothing here demands. The table survives the choice because it is not a
# QUEUE, it is the export REGISTER `design/02` also asked for — "every export
# in `audit_log`" needs a row an operator can list and re-download, not only
# a terse audit line — so `status` only ever reaches a TERMINAL value within
# the same request that created the row: "queued"/"processing" are never
# written and are not in `EXPORT_STATUSES`. If an async path is ever built,
# widen this CHECK in that migration, not before.
EXPORT_FORMATS = ("pdf", "xlsx")
EXPORT_STATUSES = ("done", "failed")


class ExportJob(Base):
    """One finished export (С22, design/02 § search: `export_jobs`).

    `params` freezes the exact filter the operator ran (`kind` plus `GET
    /search`'s own query parameters) — the evidentiary point of this table:
    months later, "what did this export actually contain" is answerable from
    this row alone, without re-running a query against data that may have
    since changed. `row_count` is how many rows the file actually holds;
    `total_matched` is how many the filter matched before the cap — equal
    unless the export was truncated, in which case the gap between them is
    what makes the truncation visible rather than silent (`app/core/
    settings_store.py::search_export_max_rows`'s own docstring). `watermarked`
    is always `True` today (ruling #20 makes it mandatory for every export
    this module produces) — carried as a real column, not a hard-coded
    constant, because a future export kind this table does not yet know
    about is exactly the kind of thing a column survives and a docstring
    does not. `file_id` is nullable for the same forward reason as `status`
    carrying `'failed'`: nothing in this implementation ever leaves it null
    (a render or storage failure raises and rolls the whole request back
    before any row exists — `error` is likewise never written today).
    """

    __tablename__ = "export_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str]
    format: Mapped[str]
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(default="done", server_default="done")
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"))
    row_count: Mapped[int | None]
    total_matched: Mapped[int | None]
    watermarked: Mapped[bool] = mapped_column(default=True, server_default="true")
    error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Synchronous by construction (see the docstring above the tuples): a row
    # only ever exists once its export has already finished, so this is
    # DB-computed at the same instant as `created_at` rather than passed in
    # from Python — the two would read identically either way, and this
    # avoids the caller needing a fresh `db.refresh()` just to report it.
    finished_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"kind IN {SEARCH_KINDS}", name="export_kind_valid"),
        CheckConstraint(f"format IN {EXPORT_FORMATS}", name="export_format_valid"),
        CheckConstraint(f"status IN {EXPORT_STATUSES}", name="export_status_valid"),
    )
