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


@pytest.fixture(autouse=True)
async def _reset_payment_recipients(engine):
    yield
    factory = make_session_factory(engine)
    async with factory() as session:
        await session.execute(
            delete(PaymentRecipient).where(PaymentRecipient.id != BUDGET_RECIPIENT_ID)
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
    response = await client.patch(
        f"/api/v1/payments/recipients/{budget_50.id}",
        json={"active": False},
        headers=sys_admin,
    )
    assert response.status_code == 200
    assert response.json()["active"] is False
    listing = await client.get("/api/v1/payments/recipients", headers=sys_admin)
    assert [r["id"] for r in listing.json()["items"]] == [str(budget_50.id)]


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
