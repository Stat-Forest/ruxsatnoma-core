"""`/api/v1/payments/recipients` — the split's own directory (stage 7.9 task
3, decisions #154/#157/#163). Reading is `payments.view` OR
`PAYMENTS_RECIPIENTS_MANAGE`; writing is `PAYMENTS_RECIPIENTS_MANAGE` alone,
which no role holds — `sys_admin` is, in practice, the only writer today
(Override 2 of that task's own brief).

`_reset_payment_recipients` below is this file's own isolation guard, local
rather than in the shared `conftest.py`: `payment_recipients` (migration
`0045`) is a brand-new table this file is the only writer of in the whole
suite, but ITS OWN tests both create rows through committed POSTs and
mutate the one seeded budget row, and the suite runs `-n 4` with no
guaranteed order between this file's own tests (lesson: "the test DB is
shared, persistent, and never empty — including the spot you picked"). Every
test here restores the directory to exactly the one seeded budget row
afterwards, through the `engine`'s own session — never `db`, whose
`rollback()` cannot undo a write the app's separate connection already
committed (mirrors `tests/modules/gis/conftest.py::_restore_gis_layers`)."""

from decimal import Decimal
from typing import get_args

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.db import make_session_factory
from app.modules.audit.models import AuditLog
from app.modules.payments.models import RECIPIENT_KINDS, PaymentRecipient
from app.modules.payments.schemas import RecipientKind
from tests.modules.payments.conftest import BUDGET_RECIPIENT_ID

API = "/api/v1"

# The exact row migration 0045 seeds — restored verbatim after every test in
# this file, regardless of what that test did to it.
_BUDGET_SEED = {
    "name": {
        "uz_latn": "Davlat byudjeti",
        "uz_cyrl": "Давлат бюджети",
        "ru": "Государственный бюджет",
    },
    "payme_account_id": None,
    "percent": Decimal("50.00"),
    "fixed_amount": None,
    "active": True,
    "sort_order": 10,
    "note": "VMQ 278 default. Payme account id to be filled in by the Agency (#159).",
}


async def _clean_up_stray_recipients(engine) -> None:
    """Deletes each stray row ONE AT A TIME, in its own SAVEPOINT, rather
    than one bulk `DELETE`: under `-n 4`, a DIFFERENT file's own test can
    call `issue_invoice` and freeze one of THIS file's just-created rows
    into an `invoice_recipients` snapshot between its creation and this
    cleanup running (cross-file interleaving the shared, persistent test DB
    makes possible — this file's own docstring names the WITHIN-file
    version of the same class). A bulk `DELETE` would then raise
    `IntegrityError` on `fk_invoice_recipients_recipient_id_payment_recipients`
    and roll back the WHOLE cleanup, leaving every stray row ACTIVE — not
    just the one actually referenced — for the rest of this WORKER's entire
    run, so a later, unrelated test's own split silently exceeds 100%
    (`ledger.SplitDoesNotFit`). Falling back to DEACTIVATE (never deleted
    again, `active=False`) only the row that could not be deleted keeps
    that failure scoped to the one row actually in use, instead of
    cascading to every stray row this file ever created."""
    factory = make_session_factory(engine)
    async with factory() as session:
        stray_ids = (
            (
                await session.execute(
                    select(PaymentRecipient.id).where(PaymentRecipient.id != BUDGET_RECIPIENT_ID)
                )
            )
            .scalars()
            .all()
        )
        for stray_id in stray_ids:
            try:
                async with session.begin_nested():
                    await session.execute(
                        delete(PaymentRecipient).where(PaymentRecipient.id == stray_id)
                    )
            except IntegrityError:
                await session.execute(
                    update(PaymentRecipient)
                    .where(PaymentRecipient.id == stray_id)
                    .values(active=False)
                )
        await session.execute(
            update(PaymentRecipient)
            .where(PaymentRecipient.id == BUDGET_RECIPIENT_ID)
            .values(**_BUDGET_SEED)
        )
        await session.commit()
        restored = await session.get(PaymentRecipient, BUDGET_RECIPIENT_ID)
    assert restored is not None
    assert restored.active is True
    assert restored.percent == Decimal("50.00")


@pytest.fixture(autouse=True)
async def _reset_payment_recipients(engine):
    """See the module docstring. Runs the cleanup BOTH before and after
    each test, not only after (Override 4, task 7): a stray row this
    file's OWN teardown could not delete (the cross-file race
    `_clean_up_stray_recipients` documents) would otherwise sit ACTIVE or
    DEACTIVATED-but-present at the START of the NEXT test too, which is
    exactly what would make `test_a_recipient_is_deactivated_not_deleted`'s
    exact-list assertion flake — a leftover from a PREVIOUS test, not
    anything the failing test itself did. Cleaning up on the way IN as well
    guarantees every test starts from exactly the one seeded budget row,
    regardless of what an earlier test (in this file, or interleaved from
    another) left behind."""
    await _clean_up_stray_recipients(engine)
    yield
    await _clean_up_stray_recipients(engine)


def test_the_schema_literal_matches_the_tables_own_check_constraint():
    """Override 4: `kind` is spelled out by hand in `RecipientKind`
    (`Literal["percent", "fixed"]`) rather than `Literal[*RECIPIENT_KINDS]`,
    which pyright rejects — this test is what keeps the two from drifting
    (mirrors `tests/modules/norms/test_models.py`'s identical guard for
    `LivestockGroup`/`LIVESTOCK_GROUPS`)."""
    assert set(get_args(RecipientKind)) == set(RECIPIENT_KINDS)


async def test_the_superuser_creates_a_percent_recipient(client, sys_admin):
    response = await client.post(
        "/api/v1/payments/recipients",
        json={
            "name": {"uz_latn": "Agentlik"},
            "kind": "percent",
            "percent": "10.00",
            "payme_account_id": "77777",
        },
        headers=sys_admin,
    )
    assert response.status_code == 201
    assert response.json()["percent"] == "10.00"


async def test_an_accountant_may_read_but_not_write(client, accountant):
    # Ruling R5: reading is payments.view OR the manage code; writing is the
    # manage code, which no role holds.
    assert (await client.get("/api/v1/payments/recipients", headers=accountant)).status_code == 200
    denied = await client.post(
        "/api/v1/payments/recipients",
        json={"name": {"uz_latn": "X"}, "kind": "percent", "percent": "1.00"},
        headers=accountant,
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "ERR-ACL-001"


async def test_percentages_above_one_hundred_in_total_are_refused(client, sys_admin, budget_50):
    response = await client.post(
        "/api/v1/payments/recipients",
        json={"name": {"uz_latn": "X"}, "kind": "percent", "percent": "60.00"},
        headers=sys_admin,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR-VAL-001"
    assert response.json()["error"]["details"]["reason"] == "percent_total_exceeds_100"


async def test_an_inactive_row_does_not_count_towards_the_hundred(
    client, sys_admin, budget_50_inactive
):
    response = await client.post(
        "/api/v1/payments/recipients",
        json={"name": {"uz_latn": "X"}, "kind": "percent", "percent": "60.00"},
        headers=sys_admin,
    )
    assert response.status_code == 201


async def test_a_recipient_is_deactivated_not_deleted(client, sys_admin, budget_50):
    """Override 4 asked for exact-list equality back (Task 6 had weakened
    this to membership) — investigating WHY it was weakened found a
    genuine, deterministic contamination, not a rare `-n 4` race: several
    OTHER files in this package build their OWN throwaway
    `PaymentRecipient` rows and commit them (`test_confirm_payment_split.py`
    ::`receiver_fixed_60000` is one), and a row that `issue_invoice` froze
    into a committed `invoice_recipients` snapshot can never be DELETEd
    again — `_clean_up_stray_recipients`'s own fallback leaves it
    DEACTIVATED but still present, forever, in this worker's database.
    Reproduced on a genuinely fresh database with NO `-n` at all
    (`pytest tests/modules/payments --fresh-db`, serial, single worker):
    12 such rows already existed by the time this test ran, from files
    collected before this one in the SAME run. `_reset_payment_recipients`
    cleaning up on the way IN as well as out (this task's own fix) does
    not change this — those rows are not stray leftovers of a PREVIOUS
    test run, they are made INSIDE this one, by fixtures this file does
    not own and may not delete out from under a test that has not run yet.

    So the exact accounting below is stronger than literal exact-list
    equality, not weaker: it captures the directory's FULL id set and every
    row's `active` flag BEFORE the call, then asserts NO row was added or
    removed and EVERY row's `active` is unchanged except `budget_50`'s own,
    which must have flipped to `False` — true regardless of how many
    permanently-deactivated strays this worker's database already carries."""
    before = await client.get("/api/v1/payments/recipients", headers=sys_admin)
    before_active = {row["id"]: row["active"] for row in before.json()["items"]}

    response = await client.patch(
        f"/api/v1/payments/recipients/{budget_50.id}",
        json={"active": False},
        headers=sys_admin,
    )
    assert response.status_code == 200
    assert response.json()["active"] is False

    after = await client.get("/api/v1/payments/recipients", headers=sys_admin)
    after_active = {row["id"]: row["active"] for row in after.json()["items"]}

    assert set(after_active) == set(before_active)
    assert after_active == {**before_active, str(budget_50.id): False}


async def test_uz_latn_is_required_in_the_name(client, sys_admin):
    response = await client.post(
        "/api/v1/payments/recipients",
        json={"name": {"ru": "Только по-русски"}, "kind": "percent", "percent": "1.00"},
        headers=sys_admin,
    )
    assert response.status_code == 422


async def test_clearing_a_percent_recipients_own_percent_is_refused(client, sys_admin, budget_50):
    """`PaymentRecipientPatch.percent` is typed `Decimal | None`, so a bare
    `null` parses — but clearing the only amount a `percent`-kind row is
    allowed to carry has no legal meaning (the `rule_matches_kind` CHECK
    would refuse the row anyway). This must come back as a clean 422, not a
    500 from the service's own internal assertion."""
    response = await client.patch(
        f"/api/v1/payments/recipients/{budget_50.id}",
        json={"percent": None},
        headers=sys_admin,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR-VAL-001"


async def test_every_change_leaves_an_audit_row_with_both_values(db, client, sys_admin, budget_50):
    await client.patch(
        f"/api/v1/payments/recipients/{budget_50.id}",
        json={"percent": "40.00"},
        headers=sys_admin,
    )
    row = (
        (
            await db.execute(
                select(AuditLog)
                .where(
                    AuditLog.object_type == "payment_recipient",
                    AuditLog.object_id == budget_50.id,
                    AuditLog.action == "payment_recipient.update",
                )
                .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.old_value["percent"] == "50.00"
    assert row.new_value["percent"] == "40.00"


# --- Fix round 1 (task-3-findings-r1.md) --------------------------------------


async def test_clearing_a_fixed_recipients_own_fixed_amount_is_refused(client, sys_admin):
    """Minor 4 — the mirror image of `test_clearing_a_percent_recipients_
    own_percent_is_refused` above: `fixed_amount` is typed `Decimal | None`
    on `PaymentRecipientPatch` too, so a bare `null` parses, and the same
    kind-mismatch guard in `recipients_service.update` must refuse it the
    same way — the two branches are symmetric enough that an asymmetric
    regression on this one would otherwise be invisible."""
    create = await client.post(
        "/api/v1/payments/recipients",
        json={"name": {"uz_latn": "Z"}, "kind": "fixed", "fixed_amount": "5000.00"},
        headers=sys_admin,
    )
    assert create.status_code == 201
    recipient_id = create.json()["id"]

    response = await client.patch(
        f"/api/v1/payments/recipients/{recipient_id}",
        json={"fixed_amount": None},
        headers=sys_admin,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR-VAL-001"


async def test_an_explicit_null_sort_order_is_refused(client, sys_admin, budget_50):
    """Important 1 — `sort_order: Mapped[int]` is NOT NULL, but
    `PaymentRecipientPatch.sort_order` is `int | None` for the "field not
    sent" idiom, so an explicit `{"sort_order": null}` used to reach the
    generic `setattr` loop and crash `db.flush()` with an uncaught
    `IntegrityError` (500, `ERR-SYS-001`) instead of a clean 422."""
    response = await client.patch(
        f"/api/v1/payments/recipients/{budget_50.id}",
        json={"sort_order": None},
        headers=sys_admin,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR-VAL-001"


async def test_an_explicit_null_active_is_refused(client, sys_admin, budget_50):
    """Important 1, the `active` half of the same gap — `active: Mapped[bool]`
    is NOT NULL, but `PaymentRecipientPatch.active` is `bool | None`, so
    `{"active": null}` used to reach `db.flush()` and raise an uncaught
    `IntegrityError` (500) rather than a 422."""
    response = await client.patch(
        f"/api/v1/payments/recipients/{budget_50.id}",
        json={"active": None},
        headers=sys_admin,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR-VAL-001"


async def test_an_explicit_null_name_is_refused(db, client, sys_admin, budget_50):
    """Override 3 of stage 7.9 task 8 — `name` is typed `LocalizedName | None`
    on `PaymentRecipientPatch` for the "field not sent" idiom, so
    `{"name": null}` parses too, and `"name" in fields` reads `True`. This
    used to silently NO-OP: `data.name is not None` was `False`, so the
    generic-looking `if "name" in fields and data.name is not None` guard
    skipped the assignment instead of applying or refusing it — a `PATCH`
    that reported 200 and changed NOTHING. It must now come back as a
    clean 422, the same shape every sibling column in this route already
    gets, and the row's own `name` must be untouched."""
    before_name = dict(budget_50.name)
    response = await client.patch(
        f"/api/v1/payments/recipients/{budget_50.id}",
        json={"name": None},
        headers=sys_admin,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERR-VAL-001"
    await db.refresh(budget_50)
    assert budget_50.name == before_name


async def test_increasing_the_sole_active_rows_own_percent_is_not_double_counted(
    client, sys_admin, budget_50
):
    """Important 2.1 — `_assert_percent_fits` must EXCLUDE the row being
    edited from the current total before adding its own new value back in
    (`exclude_id=row.id`). A DECREASE alone can never catch a broken
    exclusion, since it always still fits under the ceiling; only an
    INCREASE past the point where self-double-counting would wrongly
    refuse it proves the exclusion actually runs. `budget_50` is the sole
    active row at 50%: patched to 90%, the correct total is 90 (<=100); with
    the exclusion broken it would see 50 (itself, still counted once) + 90
    = 140 and wrongly 422."""
    response = await client.patch(
        f"/api/v1/payments/recipients/{budget_50.id}",
        json={"percent": "90.00"},
        headers=sys_admin,
    )
    assert response.status_code == 200
    assert response.json()["percent"] == "90.00"


async def test_reactivating_a_row_that_would_exceed_the_ceiling_is_refused(
    client, sys_admin, budget_50_inactive
):
    """Important 2.2 — a row being ACTIVATED must be INCLUDED in the total
    (`resulting_active` branch), not left out the way an untouched inactive
    row is. `budget_50_inactive` starts deactivated, so the active total is
    0% and a fresh 60% row may be created; reactivating the budget row on
    top of it (60 + 50 = 110) must be refused with the same ceiling error a
    straight creation over 100 gets."""
    create = await client.post(
        "/api/v1/payments/recipients",
        json={"name": {"uz_latn": "Y"}, "kind": "percent", "percent": "60.00"},
        headers=sys_admin,
    )
    assert create.status_code == 201

    reactivate = await client.patch(
        f"/api/v1/payments/recipients/{budget_50_inactive.id}",
        json={"active": True},
        headers=sys_admin,
    )
    assert reactivate.status_code == 422
    assert reactivate.json()["error"]["code"] == "ERR-VAL-001"
    assert reactivate.json()["error"]["details"]["reason"] == "percent_total_exceeds_100"
