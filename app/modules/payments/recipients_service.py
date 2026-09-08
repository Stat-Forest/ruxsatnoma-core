"""The recipients directory's own service (stage 7.9 task 3, decisions #154,
#157, #163) — the CRUD side of the configurable split. `active_rules` below
is the ONE reader Task 4 freezes onto an invoice at issuance; every other
reader (this file's own `list_all`, a future report) goes through
`repo.list_payment_recipients` instead, so that editing the directory can
never change what an already-issued invoice divides into.

**The audit trail is this directory's WHOLE history mechanism, by design**
(decision #164, ruling R6). `payment_recipients` is NOT versioned with
effective periods the way `norms` versions its tariffs: "which percentage
applied to THIS payment" is answered by the invoice's own frozen
`invoice_recipients` snapshot (Task 4), which is more accurate than a
calendar lookup because it records what actually applied, not what should
have applied on that date. `audit_log` answers the other question — who
changed the directory, when, and from what to what — and it is
append-only at the database level, so that answer cannot be edited away
after the fact. Every `create`/`update` call below writes exactly one
`audit.log` row with BOTH `old_value` and `new_value`, in the SAME
transaction as the write it describes (design/01 rule 6) — an audit row
that silently stops being written is invisible to every other check in this
module, which is why `test_every_change_leaves_an_audit_row_with_both_values`
exists at all."""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.schemas import Page, PageParams
from app.modules.audit import service as audit
from app.modules.auth.models import User
from app.modules.payments import ledger, repo
from app.modules.payments.models import PaymentRecipient
from app.modules.payments.schemas import (
    PaymentRecipientIn,
    PaymentRecipientOut,
    PaymentRecipientPatch,
)

HUNDRED = Decimal("100")


def _snapshot(row: PaymentRecipient) -> dict[str, Any]:
    """The audit-row shape for one `PaymentRecipient` — every column a
    mutation can touch, `Decimal`/`UUID`/`datetime` coerced to strings so the
    JSONB write never sees a type it cannot serialize (mirrors
    `legal_documents_service._snapshot`)."""
    return {
        "name": row.name,
        "payme_account_id": row.payme_account_id,
        "kind": row.kind,
        "percent": None if row.percent is None else str(row.percent),
        "fixed_amount": None if row.fixed_amount is None else str(row.fixed_amount),
        "active": row.active,
        "sort_order": row.sort_order,
        "note": row.note,
    }


async def _assert_percent_fits(
    db: AsyncSession, *, exclude_id: uuid.UUID | None, resulting_percent: Decimal
) -> None:
    """The percentage total across ACTIVE rows may not exceed 100 — the row
    being edited (`exclude_id`) is excluded from the CURRENT total before
    `resulting_percent` (what that same row would carry once this call
    succeeds) is added back in. A row being newly created or newly activated
    passes `exclude_id=None`/its own not-yet-active id, which this exclusion
    handles identically: there is nothing of its own yet counted to leave
    out."""
    active_rows = await repo.list_active_recipients(db)
    total = sum(
        (row.percent for row in active_rows if row.id != exclude_id and row.percent is not None),
        Decimal("0"),
    )
    if total + resulting_percent > HUNDRED:
        raise err("ERR-VAL-001", details={"reason": "percent_total_exceeds_100"})


def _to_out(row: PaymentRecipient) -> PaymentRecipientOut:
    return PaymentRecipientOut.model_validate(row)


async def list_all(db: AsyncSession, *, params: PageParams) -> Page[PaymentRecipientOut]:
    """The whole directory, active and inactive — an inactive row is never
    hidden from this list (decision #157: it is deactivated, never deleted,
    and a reader must be able to see what used to apply)."""
    rows, total = await repo.list_payment_recipients(
        db, limit=params.page_size, offset=params.offset
    )
    items = [_to_out(row) for row in rows]
    return Page[PaymentRecipientOut](
        items=items, total=total, page=params.page, page_size=params.page_size
    )


async def create(db: AsyncSession, *, data: PaymentRecipientIn, actor: User) -> PaymentRecipientOut:
    """A new row is always created ACTIVE (the model's own default; there is
    no `active` field on `PaymentRecipientIn` at all) — so a `percent` row
    always contributes to the 100% ceiling from the moment it exists."""
    if data.kind == "percent":
        assert data.percent is not None  # PaymentRecipientIn._one_rule_only guarantees this
        await _assert_percent_fits(db, exclude_id=None, resulting_percent=data.percent)
    row = PaymentRecipient(
        name=data.name.root,
        payme_account_id=data.payme_account_id,
        kind=data.kind,
        percent=data.percent,
        fixed_amount=data.fixed_amount,
        sort_order=data.sort_order,
        note=data.note,
        created_by=actor.id,
    )
    await repo.add_payment_recipient(db, row)
    await audit.log(
        db,
        action="payment_recipient.create",
        user_id=actor.id,
        object_type="payment_recipient",
        object_id=row.id,
        new_value=_snapshot(row),
    )
    return _to_out(row)


async def _recipient_or_404(db: AsyncSession, recipient_id: uuid.UUID) -> PaymentRecipient:
    row = await repo.get_payment_recipient(db, recipient_id)
    if row is None:
        raise err("ERR-SYS-003", details={"payment_recipient": str(recipient_id)})
    return row


async def update(
    db: AsyncSession, *, recipient_id: uuid.UUID, data: PaymentRecipientPatch, actor: User
) -> PaymentRecipientOut:
    """`PATCH /payments/recipients/{id}` — only the keys actually sent are
    touched (`exclude_unset=True`). `kind` never changes, so `percent`/
    `fixed_amount` here must match the row's OWN kind, checked before either
    is written (the same "refuse before the database does" reasoning
    `PaymentRecipientIn._one_rule_only` applies at creation, restated here
    because a `PATCH` body has no `kind` field of its own to validate
    against). An explicit `null` for the row's OWN amount field is refused
    the same way a wrong-kind field is: `PaymentRecipientPatch` types both
    fields `Decimal | None` so pydantic accepts a bare `null`, but clearing
    the only amount a `rule_matches_kind` row is allowed to carry has no
    legal meaning here — the DB CHECK would refuse it anyway, and refusing
    it here turns that into a clean 422 instead of a 500 from the
    `resulting_percent is not None` assertion below."""
    row = await _recipient_or_404(db, recipient_id)
    fields = data.model_dump(exclude_unset=True)
    before = _snapshot(row)

    if "percent" in fields and (row.kind != "percent" or fields["percent"] is None):
        raise err("ERR-VAL-001", details={"reason": "invalid_percent_for_recipient_kind"})
    if "fixed_amount" in fields and (row.kind != "fixed" or fields["fixed_amount"] is None):
        raise err("ERR-VAL-001", details={"reason": "invalid_fixed_amount_for_recipient_kind"})

    resulting_active = fields.get("active", row.active)
    if row.kind == "percent" and resulting_active:
        resulting_percent = fields.get("percent", row.percent)
        assert resulting_percent is not None  # rule_matches_kind: a percent row always has one
        await _assert_percent_fits(db, exclude_id=row.id, resulting_percent=resulting_percent)

    if "name" in fields and data.name is not None:
        row.name = data.name.root
    for field in ("payme_account_id", "percent", "fixed_amount", "sort_order", "note", "active"):
        if field in fields:
            setattr(row, field, fields[field])

    await db.flush()
    await audit.log(
        db,
        action="payment_recipient.update",
        user_id=actor.id,
        object_type="payment_recipient",
        object_id=row.id,
        old_value=before,
        new_value=_snapshot(row),
    )
    return _to_out(row)


async def set_active(
    db: AsyncSession, *, recipient_id: uuid.UUID, active: bool, actor: User
) -> PaymentRecipientOut:
    """The semantic name for decision #157's "deactivated, never deleted" —
    a bare `{"active": ...}` patch, routed through `update` so the
    percent-total check on (re)activation and the single audit row per call
    never have a second implementation to drift from."""
    return await update(
        db, recipient_id=recipient_id, data=PaymentRecipientPatch(active=active), actor=actor
    )


async def active_rules(db: AsyncSession) -> list[ledger.RecipientRule]:
    """The ACTIVE directory rows, in `(sort_order, id)` order, as the pure
    engine's input. This is the ONE reader Task 4 freezes onto an invoice —
    every other caller reads the frozen snapshot instead, so that an edit
    here can never change what an already-issued invoice divides into
    (decision #158)."""
    rows = await repo.list_active_recipients(db)
    return [ledger.RecipientRule(row.id, row.kind, row.percent, row.fixed_amount) for row in rows]
