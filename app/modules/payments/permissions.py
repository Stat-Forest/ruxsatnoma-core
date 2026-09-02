"""Permission codes owned by payments; importing registers them (same idiom as
app/modules/applications/permissions.py; registration itself is a stand-in import
in app/main.py until Task 2's router.py exists and imports this module directly).

Migration 0017 grants both codes to `accountant` (`roles.code = 'accountant'`,
«Бухгалтер» — seeded by 0003_auth.py; verified against that migration, not prose,
per the lesson on role codes). `sys_admin` needs no explicit grant: it is a
superuser and `require_permission` lets it through before checking codes
(`CLAUDE.md`, decision #41 ruling 2)."""

from app.modules.auth.permissions import register

PAYMENTS_VIEW = "payments.view"
PAYMENTS_MANAGE = "payments.manage"

register(
    {
        PAYMENTS_VIEW: "See invoices and the payment ledger beyond one's own",
        PAYMENTS_MANAGE: "Accountant actions on invoices and payments",
    }
)
