"""Search — a level-5 reader (design/01 rule 5, design/02 § search).

Only ONE table belongs to this module: `saved_filters` (a user's own saved
search profile, optionally shared with roles/users). The actual search itself
reads `applications`/`permits`/`applicants` directly through `search/repo.py`
(the reader exception) and writes nothing there — see `04.5-4.7-search-
archive.md` ruling 1 for why this module carries no tsvector column of its
own on those tables (it would have to be declared in THEIR `models.py`, which
a reader may never edit).
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
