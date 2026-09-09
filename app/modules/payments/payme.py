"""The Payme JSON-RPC dispatcher — all seven methods design/04 §3.2 lists
(design/02 § payments, plan `03.10a-payments-core` tasks 4-5,
`design/04-integrations.md` §3 end to end). Task 4 shipped the five
transactional methods; task 5 adds the two reporting/maintenance ones,
`GetStatement` and `ChangePassword`.

`handle(db, method, params, *, now)` is the ONLY public name: **no HTTP, no
auth, no framework** — every state and every error code here is
unit-testable without a client (`payme_router.py` is the thin always-200
shell that calls this and renders `PaymeError` into a 200 response). `now`
is always injected (ruling G): this module never reads the wall clock
itself, so the 12h timeout is provably measured from `received_at` alone,
not from a call to `datetime.now()` buried three functions deep.

Two error-code families this module uses, per `design/04-integrations.md`
§3.6 (corrected by the whole-branch review, finding I2 — this supersedes
ruling F's own narrower split, which put BOTH "unknown" and "found but not
payable" under `-31050`):

- `-31050` ("account error") is what `CheckPerformTransaction` and
  `CreateTransaction` answer when the invoice NUMBER itself does not
  resolve to a row at all.
- `-31008` ("cannot perform") is what BOTH methods answer when the invoice
  IS found but `status != "pending"` (already paid, cancelled, expired) —
  real Payme semantics are that `CreateTransaction` re-runs
  `CheckPerformTransaction`'s own check and must return the identical
  error for the identical invoice; two callers disagreeing about the same
  row is the exact bug class this review finding closed. `-31008` is ALSO
  what `PerformTransaction` answers for an EXISTING `provider_transactions`
  row that can no longer be acted on — expired past the 12h timeout
  (`CreateTransaction`'s idempotent-replay branch, `PerformTransaction`), or
  whose invoice left `pending` AFTER the transaction was created
  (`PerformTransaction` only — ruling F's restructured
  `test_performing_against_a_cancelled_invoice_answers_31008`, the real
  race: the applicant withdrew between `CreateTransaction` and now). One
  code, two related but distinct triggers — "this account cannot be paid"
  and "this transaction cannot proceed" — never conflated with `-31050`,
  which is reserved for "this account does not exist at all".

Stage 7.9 task 6 (decision #160): `-31008` is now ALSO what
`CheckPerformTransaction` and `CreateTransaction` answer when the invoice's
own FROZEN split (`service.invoice_recipients`, decision #158) cannot be
routed at Payme in full — some receiver that would actually get money
carries no `payme_account_id` (`_receivers_for`). Fail-closed by design: a
payment we cannot route is refused rather than taken onto the Agency's own
cashbox for someone to move by hand, and the refusal is ALL-OR-NOTHING —
never a shortened `receivers` array. `CreateTransaction`'s own response
gains `receivers` (design/04 §3.7): one `{"id", "amount"}` per row of the
snapshot with `amount > 0`, tiyin integers via `to_tiyin`. The sibling
refusal a human actually SEES is `ERR-PAY-007` on
`POST /invoices/{id}/pay-intents` (`payments.service.create_pay_intent`) —
this module answers Payme, that one answers the citizen, and neither reuses
the other's envelope (this module is never inside `ERR-*`; `pay-intents`
is never a raw Payme code).
"""

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.modules.audit import service as audit
from app.modules.integrations.adapters.payme import (
    CASHBOX_KEY_HASH_SETTING,
    from_tiyin,
    to_tiyin,
)
from app.modules.payments import repo, service
from app.modules.payments.models import Invoice, ProviderTransaction

logger = structlog.get_logger(__name__)

PROVIDER = "payme"

# provider_transactions.state (free text, design/04 §3.3) — this module is the
# only writer, so these four literals are the whole universe in practice even
# though nothing at the DB level constrains it (model docstring).
STATE_CREATED = "1"
STATE_PERFORMED = "2"
STATE_CANCELLED_BEFORE = "-1"
STATE_CANCELLED_AFTER = "-2"

# design/04 §3.5
REASON_TIMEOUT = 4
REASON_UNKNOWN = 10

# design/04 §3.4: 43,200,000 ms, measured from OUR `received_at`, never Payme's
# own `time` param.
TRANSACTION_TIMEOUT = timedelta(hours=12)

# design/04 §3.6 (this module's own share; -32300/-32700/-32600 are
# `payme_router.py`'s — see its own module docstring)
ERR_INTERNAL = -32400
ERR_INSUFFICIENT_PRIVILEGE = -32504
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_AMOUNT = -31001
ERR_TRANSACTION_NOT_FOUND = -31003
ERR_CANNOT_CANCEL = -31007
ERR_CANNOT_PERFORM = -31008
ERR_ACCOUNT = -31050  # brief's own range is -31050..-31099; this module always answers -31050

# Audit action constants (CLAUDE.md invariant) — this module IS the acting
# code for every `provider_transactions` mutation; `payments.service` owns
# the sibling `invoice.pay` for the invoice/ledger/application side of a
# confirmed payment (see `service.confirm_payment`).
TRANSACTION_CREATE = "transaction.create"
TRANSACTION_CANCEL = "transaction.cancel"
CASHBOX_KEY_ROTATE = "payme_cashbox_key.rotate"


class PaymeError(Exception):
    """A Payme JSON-RPC error — code, message, optional data
    (design/04 §3.6). `payme_router.py` catches this and renders it into an
    HTTP 200 body; nothing on this module's happy or documented-unhappy path
    ever raises anything else."""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


def _to_millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _from_millis(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _account_number(params: dict[str, Any]) -> str | None:
    account = params.get("account")
    if not isinstance(account, dict):
        return None
    value = account.get("id")
    return value if isinstance(value, str) and value else None


def _amount_tiyin(params: dict[str, Any]) -> int | None:
    value = params.get("amount")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _external_id(params: dict[str, Any]) -> str | None:
    value = params.get("id")
    return value if isinstance(value, str) and value else None


def _cancel_reason(params: dict[str, Any]) -> int:
    value = params.get("reason")
    if isinstance(value, bool) or not isinstance(value, int):
        return REASON_UNKNOWN
    return value


async def _check_invoice_for_payment(db: AsyncSession, params: dict[str, Any]) -> Invoice:
    """`CheckPerformTransaction`'s own rule, reused VERBATIM by
    `CreateTransaction` — the two must agree, or a caller could be told
    "yes, payable" by one and refused by the other for the identical
    request. Review finding I2: "not found" and "found but not payable"
    are DIFFERENT codes (see the module docstring) — collapsing them into
    one account error told a citizen whose invoice was already paid or
    expired that the ACCOUNT NUMBER was wrong, prompting them to re-enter
    it, instead of that the payment cannot be performed."""
    number = _account_number(params)
    invoice = await repo.get_invoice_by_number(db, number) if number is not None else None
    if invoice is None:
        raise PaymeError(ERR_ACCOUNT, "Invoice not found")
    if invoice.status != "pending":
        raise PaymeError(ERR_CANNOT_PERFORM, "Invoice is not payable")
    amount = _amount_tiyin(params)
    if amount is None or amount != to_tiyin(invoice.amount):
        raise PaymeError(ERR_INVALID_AMOUNT, "Incorrect amount")
    return invoice


async def _receivers_for(db: AsyncSession, invoice_id: uuid.UUID) -> list[dict[str, Any]] | None:
    """`receivers` for `CreateTransaction`'s response (design/04 §3.7):
    `{"id": <payme_account_id>, "amount": <tiyin, integer>}` per row of the
    invoice's own FROZEN split (`service.invoice_recipients`, decision
    #158), `position` order. RAISES `PaymeError(-31008)` when
    `service.missing_payme_receivers` finds any row that would actually
    receive money with no Payme account id — decision #160, fail-closed: a
    payment whose split cannot be routed IN FULL is refused rather than
    taken onto the Agency's cashbox for someone to move by hand.
    ALL-OR-NOTHING by construction (never a shortened array): a partial
    `receivers` array would route SOME receivers at Payme and leave the
    rest on the cashbox awaiting a manual transfer — the worst of both
    mechanisms and the hardest thing in this system to reconcile.

    Called from BOTH `_check_perform_transaction` and `_create_transaction`
    below (Override 1 of the task-6 brief): Payme calls
    `CheckPerformTransaction` before it ever shows the payment page, so
    refusing there too — not only in `CreateTransaction` — is what stops a
    citizen from reaching a page that would fail anyway.

    Returns `None` — never `[]` — for an EMPTY snapshot (an invoice issued
    BEFORE stage 7.9, Override 3): it is payable unchanged, and refusing it
    would strand every invoice already pending on the stand. The
    distinction is load-bearing, not cosmetic: `_create_result` (below)
    includes the `receivers` key ONLY when this is not `None`, so `None`
    here is what keeps a legacy invoice's response from gaining an empty
    `"receivers": []` it never had. A snapshot that EXISTS but whose every
    row happens to be `0.00` (the invoice itself is `0.00` — vanishingly
    rare, never a legacy invoice) is a real, if empty, `[]`, correctly
    included."""
    snapshot = await service.invoice_recipients(db, invoice_id)
    if not snapshot:
        return None
    missing = service.missing_payme_receivers(snapshot)
    if missing:
        logger.error(
            "payme_account_id missing for invoice %s rows %s - payment refused",
            invoice_id,
            [row.position for row in missing],
        )
        raise PaymeError(ERR_CANNOT_PERFORM, "Split cannot be routed")
    return [
        {"id": row.payme_account_id, "amount": to_tiyin(row.amount)}
        for row in snapshot
        if row.amount > 0
    ]


async def _check_perform_transaction(
    db: AsyncSession, params: dict[str, Any], now: datetime
) -> dict[str, Any]:
    invoice = await _check_invoice_for_payment(db, params)
    # Discards the result — CheckPerformTransaction only ever answers
    # `{"allow": True}` or an error; `receivers` belongs solely to
    # CreateTransaction's own response (design/04 §3.7). Called for its
    # RAISE alone (Override 1: refuse here too, before the payment page).
    await _receivers_for(db, invoice.id)
    return {"allow": True}


async def _expire_if_overdue(
    db: AsyncSession, transaction: ProviderTransaction, now: datetime
) -> bool:
    """If `transaction` is still state `1` and older than the 12h timeout
    (measured from `received_at`, ruling G), cancel it with reason `4` and
    return `True`. Never touches an already-terminal transaction — the
    caller decides what an already-cancelled/performed transaction means on
    its own path."""
    if transaction.state != STATE_CREATED or now - transaction.received_at <= TRANSACTION_TIMEOUT:
        return False
    transaction.state = STATE_CANCELLED_BEFORE
    transaction.cancelled_at = now
    transaction.cancel_reason = REASON_TIMEOUT
    await audit.log(
        db,
        action=TRANSACTION_CANCEL,
        object_type="provider_transaction",
        object_id=transaction.id,
        old_value={"state": STATE_CREATED},
        new_value={"state": STATE_CANCELLED_BEFORE, "reason": REASON_TIMEOUT},
        basis="timeout",
    )
    return True


def _create_result(
    transaction: ProviderTransaction, receivers: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """`receivers` is included ONLY when not `None` (design/04 §3.7): a
    `CheckTransaction`/`GetStatement`-shaped caller never asks for it, and
    an idempotent repeat of `CreateTransaction` must answer byte-identical
    JSON to its own first call (Override 5) — an explicit `None` sentinel
    keeps an accidental empty list (a real, valid `receivers` answer for a
    fully-fixed split with nothing left over) from being read the same as
    "omit the key"."""
    result: dict[str, Any] = {
        "create_time": _to_millis(transaction.received_at),
        "transaction": str(transaction.id),
        "state": int(transaction.state),
    }
    if receivers is not None:
        result["receivers"] = receivers
    return result


async def _create_transaction_existing(
    db: AsyncSession, transaction: ProviderTransaction, now: datetime
) -> dict[str, Any]:
    if await _expire_if_overdue(db, transaction, now):
        raise PaymeError(ERR_CANNOT_PERFORM, "Transaction has expired")
    if transaction.state in (STATE_CREATED, STATE_PERFORMED):
        # Override 5: a repeat answers byte-identical JSON. `receivers` is
        # RECOMPUTED here, never cached from the original call — the
        # snapshot it reads (`service.invoice_recipients`) is frozen at
        # issuance and never changes, so recomputing is provably stable,
        # and caching would be one more piece of state to keep in sync with
        # nothing.
        receivers = await _receivers_for(db, transaction.invoice_id)
        return _create_result(transaction, receivers)
    raise PaymeError(ERR_CANNOT_PERFORM, "Transaction was already cancelled")


async def _create_transaction(
    db: AsyncSession, params: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Idempotent by Payme's `id` (ruling 5 / design/04 §3.9): a verbatim
    retry returns the SAME stored transaction, never a second row —
    `uq_provider_transactions_external` is the backstop for a genuine
    concurrent race between two different requests, never the primary
    mechanism (mirrors `payments.service.issue_invoice`'s own reasoning
    about its own unique index)."""
    external_id = _external_id(params)
    if external_id is None:
        raise PaymeError(ERR_TRANSACTION_NOT_FOUND, "Transaction id is required")

    existing = await repo.get_provider_transaction_by_external_id_for_update(
        db, PROVIDER, external_id
    )
    if existing is not None:
        return await _create_transaction_existing(db, existing, now)

    invoice = await _check_invoice_for_payment(db, params)
    # Computed BEFORE any row is written (decision #160, fail-closed): an
    # invoice whose split cannot be routed must never get a
    # `provider_transactions` row at all — raises `PaymeError(-31008)` here,
    # leaving nothing behind for a caller to clean up.
    receivers = await _receivers_for(db, invoice.id)
    amount = _amount_tiyin(params)
    assert amount is not None  # _check_invoice_for_payment already validated it

    transaction = ProviderTransaction(
        invoice_id=invoice.id,
        provider=PROVIDER,
        external_id=external_id,
        amount=from_tiyin(amount),  # ruling I: the money that arrived, not invoice.amount
        state=STATE_CREATED,
        received_at=now,
        performed_at=None,
        payload=dict(params),
    )
    try:
        # SAVEPOINT, not a bare flush (lesson: "A failed DB statement aborts
        # the whole transaction — catch the right type, recover with a
        # SAVEPOINT" — mirrors signatures.service.sign()'s insert-race template).
        async with db.begin_nested():
            await repo.add_provider_transaction(db, transaction)
    except IntegrityError as exc:
        cause = exc.orig.__cause__ if exc.orig is not None else None
        if getattr(cause, "constraint_name", None) != "uq_provider_transactions_external":
            raise
        # Lost a race with a concurrent, verbatim retry of this exact call
        # (design/04 §3.9) — the OTHER request's row is now the truth.
        raced = await repo.get_provider_transaction_by_external_id_for_update(
            db, PROVIDER, external_id
        )
        if raced is None:  # pragma: no cover - the constraint just fired on this exact key
            raise
        return await _create_transaction_existing(db, raced, now)

    await audit.log(
        db,
        action=TRANSACTION_CREATE,
        object_type="provider_transaction",
        object_id=transaction.id,
        new_value={
            "invoice_id": str(invoice.id),
            "external_id": external_id,
            "amount": str(transaction.amount),
        },
    )
    return _create_result(transaction, receivers)


def _perform_result(transaction: ProviderTransaction) -> dict[str, Any]:
    return {
        "transaction": str(transaction.id),
        "perform_time": _to_millis(transaction.performed_at) if transaction.performed_at else 0,
        "state": int(transaction.state),
    }


async def _perform_transaction(
    db: AsyncSession, params: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """state `1` -> `2`: invoice paid, ledger written, application PAID,
    `payment_confirmed` published (`service.confirm_payment`, ruling J) — a
    repeat for an already-performed transaction returns the stored result,
    with NO second write (idempotent, ruling 5)."""
    external_id = _external_id(params)
    transaction = (
        await repo.get_provider_transaction_by_external_id_for_update(db, PROVIDER, external_id)
        if external_id is not None
        else None
    )
    if transaction is None:
        raise PaymeError(ERR_TRANSACTION_NOT_FOUND, "Transaction not found")

    if transaction.state == STATE_PERFORMED:
        return _perform_result(transaction)

    if transaction.state != STATE_CREATED:
        raise PaymeError(ERR_CANNOT_PERFORM, "Transaction cannot be performed")

    if await _expire_if_overdue(db, transaction, now):
        raise PaymeError(ERR_CANNOT_PERFORM, "Transaction has expired")

    invoice = await repo.get_invoice_for_update(db, transaction.invoice_id)
    if invoice is None or invoice.status != "pending":
        # Ruling F / design/04 ruling 16 half 2: the applicant may have
        # cancelled between CreateTransaction and now — refuse rather than
        # forcing an illegal CANCELLED -> PAID application transition inside
        # a route that must always answer 200.
        raise PaymeError(ERR_CANNOT_PERFORM, "Invoice is not payable")

    transaction.state = STATE_PERFORMED
    transaction.performed_at = now
    await service.confirm_payment(db, invoice=invoice, transaction=transaction)
    return _perform_result(transaction)


def _cancel_result(transaction: ProviderTransaction) -> dict[str, Any]:
    return {
        "transaction": str(transaction.id),
        "cancel_time": _to_millis(transaction.cancelled_at) if transaction.cancelled_at else 0,
        "state": int(transaction.state),
        "reason": transaction.cancel_reason,
    }


async def _cancel_transaction(
    db: AsyncSession, params: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """state `1` -> `-1`; state `2` -> `-2`, reason recorded either way.

    A state-`2` cancellation is money that was already confirmed going back
    (`design/04` §3.5 reason `5` is literally "funds returned"), and since
    3.10b it does more than move the transaction row: it calls
    `service.record_reversal`, which writes the negating `correction`
    entries, opens a `reconciliations` row and raises RI-01 (plus RI-10, and
    a direct notify to the permit's `executor_head`, when a permit already
    exists — ruling #112). 3.10a reversed nothing at all, and this docstring
    said so — see `service.py`'s public-surface banner for what shipped and
    for the one thing still open.

    **It is still logged at ERROR**, not written off as ordinary traffic
    (whole-branch review), and the reason is now narrower but real: a
    reversal is a manual human act on live money, and neither the invoice
    nor the application moves off `paid`/`PAID` (ruling 15 — `tz/05` gives
    PAID no other exit, and adding one is stage 3.9's). Until an operator
    works the register row, `service.is_paid` still answers `True` and
    `permits.service.issue`, which gates on the APPLICATION's own status,
    would still issue against it.

    A state-`1` cancellation reverses nothing, because nothing arrived: no
    ledger row, no register row, no risk indicator (pinned by
    `tests/modules/payments/test_reversal.py`)."""
    external_id = _external_id(params)
    transaction = (
        await repo.get_provider_transaction_by_external_id_for_update(db, PROVIDER, external_id)
        if external_id is not None
        else None
    )
    if transaction is None:
        raise PaymeError(ERR_TRANSACTION_NOT_FOUND, "Transaction not found")

    reason = _cancel_reason(params)

    if transaction.state in (STATE_CREATED, STATE_PERFORMED):
        old_state = transaction.state
        new_state = STATE_CANCELLED_BEFORE if old_state == STATE_CREATED else STATE_CANCELLED_AFTER
        transaction.state = new_state
        transaction.cancelled_at = now
        transaction.cancel_reason = reason
        await audit.log(
            db,
            action=TRANSACTION_CANCEL,
            object_type="provider_transaction",
            object_id=transaction.id,
            old_value={"state": old_state},
            new_value={"state": new_state, "reason": reason},
        )
        if old_state == STATE_PERFORMED:
            # Money that was already confirmed is going back. ERROR, not
            # info: the invoice and the application still say "paid" after
            # this returns (ruling 15), so the register row and this line
            # are what tell a human it happened.
            logger.error(
                "payme.cancel_after_perform",
                transaction_id=str(transaction.id),
                invoice_id=str(transaction.invoice_id),
                external_id=transaction.external_id,
                reason=reason,
            )
            # AFTER the transaction row is updated, so the reversal is
            # recorded against a row that already reads `-2`. A plain read of
            # the invoice, not `get_invoice_for_update`: nothing here writes
            # to `invoices` — the ledger, the register and the audit trail
            # are the whole of it.
            invoice = await repo.get_invoice(db, transaction.invoice_id)
            if invoice is None:  # pragma: no cover - NOT NULL FK
                raise PaymeError(ERR_CANNOT_CANCEL, "Invoice not found")
            await service.record_reversal(
                db, invoice=invoice, transaction=transaction, reason=reason
            )
    elif transaction.state not in (STATE_CANCELLED_BEFORE, STATE_CANCELLED_AFTER):
        # Invariant guard, not a documented Payme trigger: this module only
        # ever writes the four states above, so this branch is unreachable
        # in practice — never trust "can't happen" silently.
        raise PaymeError(ERR_CANNOT_CANCEL, "Transaction cannot be cancelled")
    # else: already cancelled — idempotent repeat, return the ORIGINAL
    # stored cancel details rather than overwriting them with this retry's.

    return _cancel_result(transaction)


async def _check_transaction(
    db: AsyncSession, params: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Reports state and timestamps; never mutates (the brief's own method
    table) — the one method that reads through the UNLOCKED helper."""
    external_id = _external_id(params)
    transaction = (
        await repo.get_provider_transaction_by_external_id(db, PROVIDER, external_id)
        if external_id is not None
        else None
    )
    if transaction is None:
        raise PaymeError(ERR_TRANSACTION_NOT_FOUND, "Transaction not found")
    return {
        "create_time": _to_millis(transaction.received_at),
        "perform_time": _to_millis(transaction.performed_at) if transaction.performed_at else 0,
        "cancel_time": _to_millis(transaction.cancelled_at) if transaction.cancelled_at else 0,
        "transaction": str(transaction.id),
        "state": int(transaction.state),
        "reason": transaction.cancel_reason,
    }


def _millis_param(params: dict[str, Any], key: str, default: int) -> int:
    """Same "malformed non-critical field -> safe default, never raise"
    idiom as `_cancel_reason` above — `GetStatement` is a REPORT, not a
    protected write, so a missing/malformed bound widens the window rather
    than failing the always-200 route."""
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


async def _get_statement(db: AsyncSession, params: dict[str, Any], now: datetime) -> dict[str, Any]:
    """`GetStatement` (design/04 §3.2): the transactions whose OWN
    `received_at` falls within `[from, to]` (millisecond epochs, §3.8's
    convention for time on this wire) — NEVER Payme's own `time` param,
    the same clock the 12h timeout is measured from. Missing/malformed
    bounds default to "everything up to `now`" rather than raising (see
    `_millis_param`). Each entry carries the same fields `CheckTransaction`
    reports plus `account`/`amount` (ruling); never mutates — the brief's
    own method table."""
    since = _from_millis(_millis_param(params, "from", 0))
    until = _from_millis(_millis_param(params, "to", _to_millis(now)))
    rows = await repo.list_provider_transactions_in_period(db, PROVIDER, since, until)
    return {
        "transactions": [
            {
                "create_time": _to_millis(transaction.received_at),
                "perform_time": (
                    _to_millis(transaction.performed_at) if transaction.performed_at else 0
                ),
                "cancel_time": (
                    _to_millis(transaction.cancelled_at) if transaction.cancelled_at else 0
                ),
                "transaction": str(transaction.id),
                "state": int(transaction.state),
                "reason": transaction.cancel_reason,
                "account": {"id": invoice_number},
                "amount": to_tiyin(transaction.amount),
            }
            for transaction, invoice_number in rows
        ]
    }


def _new_cashbox_key(params: dict[str, Any]) -> str | None:
    value = params.get("password")
    return value if isinstance(value, str) and value else None


async def _change_password(
    db: AsyncSession, params: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """`ChangePassword` (design/04 §3.2): rotates the cashbox key
    `integrations.adapters.payme._CashboxKeyAdapter.verify` authenticates
    future calls against. Persists `sha256(new key)` in `settings_store`'s
    `payme_cashbox_key_hash` override — NEVER the plaintext (ruling): a live
    payment secret has no business sitting in an admin-readable table, and
    returning success while dropping the key would silently kill every
    later call with `-32504`. `set_setting` writes with no actor
    (`settings_store` is level 0 — a Payme call has no `User`); this module
    is the acting code, so IT invalidates the cache and audits, with
    `user_id=None` (the job idiom — there is no HTTP actor on this route
    either). The stored-hash cache is per-PROCESS and expires after 60s
    (`settings_store` module docstring), so a rotation reaches every uvicorn
    worker within a minute; Payme retries on a wrong-credentials response,
    so this self-heals rather than needing a broadcast."""
    new_key = _new_cashbox_key(params)
    if new_key is None:
        raise PaymeError(ERR_INSUFFICIENT_PRIVILEGE, "New password is required")
    new_hash = hashlib.sha256(new_key.encode("utf-8")).hexdigest()
    await settings_store.set_setting(db, CASHBOX_KEY_HASH_SETTING, new_hash)
    settings_store.invalidate(CASHBOX_KEY_HASH_SETTING)
    await audit.log(
        db,
        action=CASHBOX_KEY_ROTATE,
        object_type="payme_cashbox_key",
        user_id=None,
        new_value={"rotated_at": _to_millis(now)},
    )
    return {"success": True}


_METHODS: dict[
    str, Callable[[AsyncSession, dict[str, Any], datetime], Awaitable[dict[str, Any]]]
] = {
    "CheckPerformTransaction": _check_perform_transaction,
    "CreateTransaction": _create_transaction,
    "PerformTransaction": _perform_transaction,
    "CancelTransaction": _cancel_transaction,
    "CheckTransaction": _check_transaction,
    "GetStatement": _get_statement,
    "ChangePassword": _change_password,
}


async def handle(
    db: AsyncSession, method: str, params: dict[str, Any], *, now: datetime
) -> dict[str, Any]:
    """The Payme JSON-RPC dispatcher (design/04 §3): all seven methods
    §3.2 lists. No HTTP, no auth, no framework — `payme_router.py` is the
    only caller."""
    handler = _METHODS.get(method)
    if handler is None:
        raise PaymeError(ERR_METHOD_NOT_FOUND, "Method not found")
    return await handler(db, params, now)
