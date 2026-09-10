"""The payments module's `.xlsx` exports (stage 13, ruling #204): one
`Row`/`columns`/`rows`/`render` group per register, all in this file (Track
C's own scope — `docs/plans/13-register-export-xlsx.md` § Track C).

Every `rows_*` function calls the SAME service function its sibling list
route calls, with the SAME filters, so the file can never hold a row the
screen would not show (ruling R2) — an id the sheet shows is resolved to a
name in one batch query per table, never per row. Status and other enum
labels are copied verbatim from the adminka's own label maps
(`src/pages/permits/statusMeta.ts`, `src/pages/accountant/statusMeta.ts`)
so the file reads like the screen.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.modules.applications import service as applications_service
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.payments import service, statement_service
from app.modules.payments.models import BankStatement, Invoice

# An unknown code renders as itself, never an empty cell that hides it —
# the same posture `applications/export.py`'s own `_label` takes.
_YES_NO: dict[xlsx.Lang, dict[bool, str]] = {
    "uz_latn": {True: "Ha", False: "Yoʻq"},
    "ru": {True: "Да", False: "Нет"},
}


def _label(table: dict[str, dict[str, str]], code: str, lang: xlsx.Lang) -> str:
    return table.get(code, {}).get(lang, code)


def _yes_no(value: bool, lang: xlsx.Lang) -> str:
    return _YES_NO[lang][value]


_ACTIVE_LABEL: dict[xlsx.Lang, str] = {"uz_latn": "Faol", "ru": "Активен"}
_INACTIVE_LABEL: dict[xlsx.Lang, str] = {"uz_latn": "Faol emas", "ru": "Неактивен"}


def _active_label(active: bool, lang: xlsx.Lang) -> str:
    return _ACTIVE_LABEL[lang] if active else _INACTIVE_LABEL[lang]


async def _invoice_numbers_by_ids(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """One query, `{}` for an empty set — the same shape as the batch
    readers Phase 0 added to the other modules, kept local here since
    `Invoice` is this module's own model (no cross-module boundary to
    cross for it)."""
    if not ids:
        return {}
    result = await db.execute(select(Invoice.id, Invoice.number).where(Invoice.id.in_(ids)))
    return {row.id: row.number for row in result}


# --- Invoices (Task C.1, `GET /invoices/export.xlsx`) -----------------------

# Mirrors `adminka/src/pages/permits/statusMeta.ts::INVOICE_STATUS_LABEL_I18N`
# (`INVOICE_STATUSES` in `payments/models.py`), checked 2026-09-11.
INVOICE_STATUS_LABELS: dict[str, dict[str, str]] = {
    "pending": {"uz_latn": "Toʻlov kutilmoqda", "ru": "Ожидает оплаты"},
    "paid": {"uz_latn": "Toʻlangan", "ru": "Оплачено"},
    "expired": {"uz_latn": "Muddati tugagan", "ru": "Истек срок"},
    "cancelled": {"uz_latn": "Bekor qilingan", "ru": "Отменено"},
}
INVOICES_TITLE: dict[str, str] = {"uz_latn": "Hisob-fakturalar", "ru": "Счета"}


class InvoiceRow:
    """One invoice plus the names the sheet shows and its settlement flag —
    the columns read attributes off this, so every batch resolver runs once
    per table, not once per row."""

    def __init__(
        self,
        invoice: Invoice,
        *,
        application_number: str,
        applicant: str,
        settled_without_payment: bool,
    ) -> None:
        self.invoice = invoice
        self.id = invoice.id
        self.application_number = application_number
        self.applicant = applicant
        self.settled_without_payment = settled_without_payment


def invoice_columns(lang: xlsx.Lang) -> list[xlsx.Column[InvoiceRow]]:
    i = lambda f: lambda r: getattr(r.invoice, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column("number", {"uz_latn": "Hisob raqami", "ru": "Номер счёта"}, i("number"), 18),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(INVOICE_STATUS_LABELS, r.invoice.status, lang),
            18,
        ),
        xlsx.Column(
            "application", {"uz_latn": "Ariza", "ru": "Заявка"}, lambda r: r.application_number, 20
        ),
        xlsx.Column(
            "applicant", {"uz_latn": "Ariza beruvchi", "ru": "Заявитель"}, lambda r: r.applicant, 30
        ),
        xlsx.Column("amount", {"uz_latn": "Summa", "ru": "Сумма"}, i("amount"), 14),
        xlsx.Column(
            "settled_without_payment",
            {"uz_latn": "Toʻlovsiz yopilgan", "ru": "Погашен без оплаты"},
            lambda r: _yes_no(r.settled_without_payment, lang),
            18,
        ),
        xlsx.Column("issued_at", {"uz_latn": "Berilgan", "ru": "Выставлен"}, i("issued_at"), 18),
        xlsx.Column("due_at", {"uz_latn": "Toʻlov muddati", "ru": "Срок оплаты"}, i("due_at"), 18),
        xlsx.Column("paid_at", {"uz_latn": "Toʻlangan", "ru": "Оплачен"}, i("paid_at"), 18),
        xlsx.id_column(),
    ]


async def invoice_rows(
    db: AsyncSession,
    *,
    actor: User,
    lang: xlsx.Lang,
    application_id: uuid.UUID | None,
    status: str | None,
) -> tuple[list[InvoiceRow], int, int]:
    """(rows, total, cap). Calls `service.list_invoices_for_actor` — the
    exact function and scope `GET /invoices` uses (ruling R2) — with the
    cap as `limit` (this list's own paging convention is `limit`/`offset`,
    not `PageParams`, so no `model_construct` is needed here)."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    invoices, total = await service.list_invoices_for_actor(
        db, application_id, actor=actor, status=status, limit=cap, offset=0
    )
    app_ids = {invoice.application_id for invoice in invoices}
    app_numbers = await applications_service.numbers_by_ids(db, app_ids)
    app_applicants = await applications_service.applicants_by_ids(db, app_ids)
    applicants = await auth_service.applicant_names(db, set(app_applicants.values()))
    rows: list[InvoiceRow] = []
    for invoice in invoices:
        applicant_id = app_applicants.get(invoice.application_id)
        rows.append(
            InvoiceRow(
                invoice,
                application_number=app_numbers.get(invoice.application_id) or "",
                applicant=applicants.get(applicant_id, "") if applicant_id else "",
                # Per its own docstring: no query at all unless the row is
                # actually `paid` and zero — cheap, kept per-row like the
                # list route's own `_invoice_out`/`list_invoices` do.
                settled_without_payment=await service.is_settled_without_payment(db, invoice),
            )
        )
    return rows, total, cap


def render_invoices(items: list[InvoiceRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, invoice_columns(lang), lang=lang, title=INVOICES_TITLE[lang])


# --- Bank statements (Task C.2, `GET /payments/bank-statements/export.xlsx`)
# Mirrors `adminka/src/pages/accountant/statusMeta.ts::STATEMENT_STATUS_LABEL_I18N`
# (`BANK_STATEMENT_STATUSES`), checked 2026-09-11. `list_statements` answers
# HEADERS only (no lines — those live under `GET /bank-statements/{id}`, a
# single-item read this export does not mirror), so `stats`/`error_report`
# stay out of the sheet (never a raw blob) and there is no per-line table
# here at all.
STATEMENT_STATUS_LABELS: dict[str, dict[str, str]] = {
    "pending": {"uz_latn": "Navbatda", "ru": "В очереди"},
    "parsing": {"uz_latn": "Qayta ishlanmoqda", "ru": "Обрабатывается"},
    "parsed": {"uz_latn": "Qayta ishlandi", "ru": "Обработано"},
    "failed": {"uz_latn": "Xatolik", "ru": "Ошибка"},
}
STATEMENT_SOURCE_LABELS: dict[str, dict[str, str]] = {
    "file": {"uz_latn": "Fayl", "ru": "Файл"},
    "api": {"uz_latn": "API", "ru": "API"},
}
STATEMENTS_TITLE: dict[str, str] = {"uz_latn": "Bank hisobotlari", "ru": "Банковские выписки"}


class StatementRow:
    def __init__(self, statement: BankStatement, *, imported_by: str) -> None:
        self.statement = statement
        self.id = statement.id
        self.imported_by = imported_by


def statement_columns(lang: xlsx.Lang) -> list[xlsx.Column[StatementRow]]:
    s = lambda f: lambda r: getattr(r.statement, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "statement_date",
            {"uz_latn": "Hisobot sanasi", "ru": "Дата выписки"},
            s("statement_date"),
            16,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(STATEMENT_STATUS_LABELS, r.statement.status, lang),
            18,
        ),
        xlsx.Column(
            "source",
            {"uz_latn": "Manba", "ru": "Источник"},
            lambda r: _label(STATEMENT_SOURCE_LABELS, r.statement.source, lang),
            12,
        ),
        xlsx.Column(
            "period_from", {"uz_latn": "Davr boshi", "ru": "Период с"}, s("period_from"), 14
        ),
        xlsx.Column("period_to", {"uz_latn": "Davr oxiri", "ru": "Период по"}, s("period_to"), 14),
        xlsx.Column(
            "imported_by", {"uz_latn": "Yuklagan", "ru": "Загрузил"}, lambda r: r.imported_by, 24
        ),
        xlsx.Column(
            "created_at", {"uz_latn": "Yaratilgan", "ru": "Загружено"}, s("created_at"), 18
        ),
        xlsx.id_column(),
    ]


async def statement_rows(
    db: AsyncSession, *, lang: xlsx.Lang, status: str | None
) -> tuple[list[StatementRow], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    statements, total = await statement_service.list_statements(
        db, status=status, limit=cap, offset=0
    )
    importer_ids = {s.imported_by for s in statements if s.imported_by}
    names = await auth_service.user_names(db, importer_ids)
    rows = [
        StatementRow(s, imported_by=names.get(s.imported_by, "") if s.imported_by else "")
        for s in statements
    ]
    return rows, total, cap


def render_statements(items: list[StatementRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, statement_columns(lang), lang=lang, title=STATEMENTS_TITLE[lang])
