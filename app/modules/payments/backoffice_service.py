"""The discrepancy register's own service half (plan
`03.10b-payments-reconciliation` task 5): listing the open (or resolved)
`reconciliations` rows Task 4's import writes, and closing one.

**There is no task table, and this stage creates none.** `tz/08` asks for
"a task for the accountant" beside the register; an OPEN `reconciliations`
row with `assigned_to` set IS that task — exactly what `design/02` gives the
column for. Do not build a second worklist on top of this one.

**Resolving never touches money.** Nothing here writes `invoices`,
`allocations` or `provider_transactions` — `statement_service.py`'s own
docstring says why (`tz/05` invariant 3: only a provider confirmation or the
maker-checker path pays an invoice). Closing a row here records that an
accountant looked at a discrepancy and explains it; it does not resolve it
financially.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.models import MediaFile
from app.modules.audit import service as audit
from app.modules.payments import repo
from app.modules.payments.models import RECONCILIATION_STATUSES, Reconciliation

RESOLVE_ACTION = "reconciliation.resolve"

# Unpacked rather than retyped: the one comparison and the one assignment
# below read this name, never a bare `"resolved"` string of their own
# (module docstring's own vocabulary rule).
_, _STATUS_RESOLVED = RECONCILIATION_STATUSES


async def list_reconciliations(
    db: AsyncSession,
    *,
    status: str,
    limit: int,
    offset: int,
    actor: Any,
) -> tuple[Sequence[Reconciliation], int]:
    """`GET /payments/reconciliations` — the register, oldest first.

    `actor` is accepted (and not used to filter) for symmetry with
    `resolve_reconciliation` and for a future personal worklist ("my own
    assigned rows") `tz/08` does not ask for today; the route's own
    `PAYMENTS_VIEW` gate already decides who may call this at all, the same
    way `statement_service.get_statement` needs no actor for an unscoped
    read (neither table carries an `organization_id` to scope on)."""
    return await repo.list_reconciliations(db, status=status, limit=limit, offset=offset)


async def _assert_doc_active(db: AsyncSession, file_id: uuid.UUID) -> None:
    """An EXISTENCE check, not a validity check (lesson) — confirms a
    `media_files` row exists and is not archived; nothing about whether it
    actually documents this discrepancy. Mirrors `norms.service.
    _assert_doc_active`/`gis.service._assert_approval_doc_active`, each a
    private helper of a sibling module this one may not import."""
    file = await db.get(MediaFile, file_id)
    if file is None or file.status != "active":
        raise err("ERR-VAL-001", details={"reason": "resolution_doc_not_active"})


async def resolve_reconciliation(
    db: AsyncSession,
    reconciliation_id: uuid.UUID,
    *,
    comment: str,
    resolution_doc_id: uuid.UUID | None,
    actor: Any,
) -> Reconciliation:
    """`POST /payments/reconciliations/{id}/resolve` — `tz/08`: close a
    discrepancy with a comment or with a correcting document.

    A blank comment (`""`, whitespace-only) is `ERR-VAL-001`: the schema's
    own `str` requirement only rules out a MISSING field, not one filled with
    spaces. An already-`resolved` row is `ERR-PAY-005` (409) rather than a
    silent second resolution overwriting the first accountant's comment —
    NOT `ERR-PAY-004`, whose registered message names an invoice, never a
    reconciliation.
    """
    stripped = comment.strip()
    if not stripped:
        raise err("ERR-VAL-001", details={"reason": "comment_required"})
    row = await repo.get_reconciliation_for_update(db, reconciliation_id)
    if row is None:
        raise err("ERR-SYS-003")
    if row.status == _STATUS_RESOLVED:
        raise err("ERR-PAY-005")
    if resolution_doc_id is not None:
        await _assert_doc_active(db, resolution_doc_id)

    old_value = {"status": row.status, "comment": row.comment}
    row.status = _STATUS_RESOLVED
    row.comment = stripped
    row.resolution_doc_id = resolution_doc_id
    row.resolved_by = actor.id
    row.resolved_at = datetime.now(UTC)
    await db.flush()
    await audit.log(
        db,
        action=RESOLVE_ACTION,
        user_id=actor.id,
        object_type="reconciliation",
        object_id=row.id,
        old_value=old_value,
        new_value={
            "status": row.status,
            "comment": row.comment,
            "resolution_doc_id": str(resolution_doc_id) if resolution_doc_id else None,
        },
        basis=stripped,
    )
    return row
