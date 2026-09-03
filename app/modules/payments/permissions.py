"""Permission codes owned by payments; importing registers them (same idiom as
app/modules/applications/permissions.py; registration itself is a stand-in import
in app/main.py until Task 2's router.py exists and imports this module directly).

Migration 0017 grants `payments.view`/`payments.manage` to `accountant`
(`roles.code = 'accountant'`, «Бухгалтер» — seeded by 0003_auth.py; verified
against that migration, not prose, per the lesson on role codes). Migration
0022 (3.10b task 1) grants `payments.confirm` to `executor_head`
(«Ваколатли шахс», the leshoz head — verified against
0016_approver_role_alignment.py) — the checker's half of the maker-checker
manual PAID (ruling 1). `sys_admin` needs no explicit grant: it is a
superuser and `require_permission` lets it through before checking codes
(`CLAUDE.md`, decision #41 ruling 2)."""

from app.modules.auth.permissions import register

PAYMENTS_VIEW = "payments.view"
PAYMENTS_MANAGE = "payments.manage"
PAYMENTS_CONFIRM = "payments.confirm"

register(
    {
        PAYMENTS_VIEW: "See invoices and the payment ledger beyond one's own",
        PAYMENTS_MANAGE: "Accountant actions on invoices and payments",
        PAYMENTS_CONFIRM: "Check (co-sign) a maker's manual payment confirmation",
    }
)
