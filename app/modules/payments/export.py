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
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.admin import service as admin_service
from app.modules.applications import service as applications_service
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.payments import backoffice_service, recipients_service, service, statement_service
from app.modules.payments.backoffice_schemas import AllocationOut
from app.modules.payments.models import (
    BankStatement,
    Invoice,
    ManualPaymentConfirmation,
    Reconciliation,
    Refund,
)
from app.modules.payments.schemas import PaymentRecipientOut

# Refunds' `basis_item_id` names an item of this classifier (migration
# `0022`; `Refund`'s own docstring) — resolved once per export, never per
# row, through `admin.service` (the cross-module reader for reference data).
REFUND_REASONS_CLASSIFIER_CODE = "refund_reasons"

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


# --- Reconciliations (Task C.2, `GET /payments/reconciliations/export.xlsx`)
# Mirrors `adminka/src/pages/accountant/statusMeta.ts::RECONCILIATION_RESULT_LABEL_I18N`
# / `RECONCILIATION_STATUS_LABEL_I18N` (`RECONCILIATION_RESULTS`/
# `RECONCILIATION_STATUSES`), checked 2026-09-11.
RECONCILIATION_RESULT_LABELS: dict[str, dict[str, str]] = {
    "matched": {"uz_latn": "Mos keldi", "ru": "Сопоставлено"},
    "discrepancy": {"uz_latn": "Nomuvofiqlik", "ru": "Расхождение"},
    "unknown": {"uz_latn": "Noma'lum", "ru": "Неизвестно"},
}
RECONCILIATION_STATUS_LABELS: dict[str, dict[str, str]] = {
    "open": {"uz_latn": "Ochiq", "ru": "Открыто"},
    "resolved": {"uz_latn": "Yopildi", "ru": "Закрыто"},
}
RECONCILIATIONS_TITLE: dict[str, str] = {"uz_latn": "Nomuvofiqliklar", "ru": "Несоответствия"}


class ReconciliationRow:
    def __init__(self, row: Reconciliation, *, invoice_number: str) -> None:
        self.row = row
        self.id = row.id
        self.invoice_number = invoice_number


def reconciliation_columns(lang: xlsx.Lang) -> list[xlsx.Column[ReconciliationRow]]:
    f = lambda name: lambda r: getattr(r.row, name)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "invoice", {"uz_latn": "Hisob-faktura", "ru": "Счёт"}, lambda r: r.invoice_number, 18
        ),
        xlsx.Column(
            "result",
            {"uz_latn": "Natija", "ru": "Результат"},
            lambda r: _label(RECONCILIATION_RESULT_LABELS, r.row.result, lang),
            16,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(RECONCILIATION_STATUS_LABELS, r.row.status, lang),
            14,
        ),
        xlsx.Column("difference", {"uz_latn": "Farq", "ru": "Разница"}, f("difference"), 14),
        xlsx.Column("comment", {"uz_latn": "Izoh", "ru": "Комментарий"}, f("comment"), 30),
        xlsx.Column("occurred_at", {"uz_latn": "Sana", "ru": "Дата"}, f("occurred_at"), 18),
        xlsx.Column(
            "resolved_at",
            {"uz_latn": "Yopilgan sana", "ru": "Дата закрытия"},
            f("resolved_at"),
            18,
        ),
        xlsx.id_column(),
    ]


async def reconciliation_rows(
    db: AsyncSession, *, actor: User, lang: xlsx.Lang, status: str
) -> tuple[list[ReconciliationRow], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    rows, total = await backoffice_service.list_reconciliations(
        db, status=status, limit=cap, offset=0, actor=actor
    )
    invoice_ids = {row.invoice_id for row in rows if row.invoice_id}
    numbers = await _invoice_numbers_by_ids(db, invoice_ids)
    out = [
        ReconciliationRow(
            row, invoice_number=numbers.get(row.invoice_id, "") if row.invoice_id else ""
        )
        for row in rows
    ]
    return out, total, cap


def render_reconciliations(items: list[ReconciliationRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(
        items, reconciliation_columns(lang), lang=lang, title=RECONCILIATIONS_TITLE[lang]
    )


# --- Manual confirmations (Task C.2, `GET /payments/manual-confirmations/export.xlsx`)
# Mirrors `adminka/src/pages/accountant/statusMeta.ts::MANUAL_CONFIRMATION_STATUS_LABEL_I18N`
# (`MANUAL_CONFIRMATION_STATUSES`), checked 2026-09-11.
MANUAL_CONFIRMATION_STATUS_LABELS: dict[str, dict[str, str]] = {
    "pending_check": {"uz_latn": "Tekshiruv kutilmoqda", "ru": "Ожидает проверки"},
    "confirmed": {"uz_latn": "Tasdiqlandi", "ru": "Подтверждено"},
    "rejected": {"uz_latn": "Rad etildi", "ru": "Отклонено"},
}
MANUAL_CONFIRMATIONS_TITLE: dict[str, str] = {
    "uz_latn": "Qoʻlda toʻlov tasdiqlari",
    "ru": "Ручные подтверждения оплаты",
}


class ManualConfirmationRow:
    def __init__(
        self, row: ManualPaymentConfirmation, *, invoice_number: str, maker: str, checker: str
    ) -> None:
        self.row = row
        self.id = row.id
        self.invoice_number = invoice_number
        self.maker = maker
        self.checker = checker


def manual_confirmation_columns(lang: xlsx.Lang) -> list[xlsx.Column[ManualConfirmationRow]]:
    f = lambda name: lambda r: getattr(r.row, name)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "invoice", {"uz_latn": "Hisob-faktura", "ru": "Счёт"}, lambda r: r.invoice_number, 18
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(MANUAL_CONFIRMATION_STATUS_LABELS, r.row.status, lang),
            20,
        ),
        xlsx.Column("amount", {"uz_latn": "Summa", "ru": "Сумма"}, f("amount"), 14),
        xlsx.Column(
            "paid_at", {"uz_latn": "Toʻlangan sana", "ru": "Дата оплаты"}, f("paid_at"), 18
        ),
        xlsx.Column("maker", {"uz_latn": "Kiritgan", "ru": "Подал"}, lambda r: r.maker, 22),
        xlsx.Column(
            "checker", {"uz_latn": "Tekshirgan", "ru": "Проверил"}, lambda r: r.checker, 22
        ),
        xlsx.Column("reason", {"uz_latn": "Sabab", "ru": "Причина"}, f("reason"), 26),
        xlsx.Column("created_at", {"uz_latn": "Yaratilgan", "ru": "Создано"}, f("created_at"), 18),
        xlsx.id_column(),
    ]


async def manual_confirmation_rows(
    db: AsyncSession, *, actor: User, lang: xlsx.Lang, status: str
) -> tuple[list[ManualConfirmationRow], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    rows, total = await backoffice_service.list_manual_confirmations(
        db, status=status, limit=cap, offset=0, actor=actor
    )
    invoice_ids = {row.invoice_id for row in rows}
    numbers = await _invoice_numbers_by_ids(db, invoice_ids)
    user_ids = {row.maker_id for row in rows} | {row.checker_id for row in rows if row.checker_id}
    names = await auth_service.user_names(db, user_ids)
    out = [
        ManualConfirmationRow(
            row,
            invoice_number=numbers.get(row.invoice_id, ""),
            maker=names.get(row.maker_id, ""),
            checker=names.get(row.checker_id, "") if row.checker_id else "",
        )
        for row in rows
    ]
    return out, total, cap


def render_manual_confirmations(items: list[ManualConfirmationRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(
        items, manual_confirmation_columns(lang), lang=lang, title=MANUAL_CONFIRMATIONS_TITLE[lang]
    )


# --- Allocations (Task C.2, `GET /payments/allocations/export.xlsx`)
# Mirrors `adminka/src/pages/accountant/statusMeta.ts::ENTRY_TYPE_LABEL_I18N`
# / `ALLOCATION_TARGET_LABEL_I18N` (`ALLOCATION_ENTRY_TYPES`/
# `ALLOCATION_TARGETS`), checked 2026-09-11.
ALLOCATION_ENTRY_TYPE_LABELS: dict[str, dict[str, str]] = {
    "payment": {"uz_latn": "Toʻlov", "ru": "Платеж"},
    "refund": {"uz_latn": "Qaytarish", "ru": "Возврат"},
    "correction": {"uz_latn": "Tuzatish (bekor qilish)", "ru": "Корректировка (отмена)"},
}
ALLOCATION_TARGET_LABELS: dict[str, dict[str, str]] = {
    "recipient": {"uz_latn": "Ijrochi (leshoz)", "ru": "Исполнитель (лесхоз)"},
    "receiver": {"uz_latn": "Qabul qiluvchi", "ru": "Получатель"},
    "other": {"uz_latn": "Boshqa", "ru": "Другое"},
}
ALLOCATIONS_TITLE: dict[str, str] = {
    "uz_latn": "Toʻlovlar taqsimoti",
    "ru": "Распределение платежей",
}


class AllocationRow:
    def __init__(self, out: AllocationOut, *, invoice_number: str) -> None:
        self.out = out
        self.id = out.id
        self.invoice_number = invoice_number


def allocation_columns(lang: xlsx.Lang) -> list[xlsx.Column[AllocationRow]]:
    f = lambda name: lambda r: getattr(r.out, name)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "invoice", {"uz_latn": "Hisob-faktura", "ru": "Счёт"}, lambda r: r.invoice_number, 18
        ),
        xlsx.Column(
            "entry_type",
            {"uz_latn": "Turi", "ru": "Тип"},
            lambda r: _label(ALLOCATION_ENTRY_TYPE_LABELS, r.out.entry_type, lang),
            18,
        ),
        xlsx.Column(
            "target",
            {"uz_latn": "Kimga", "ru": "Кому"},
            lambda r: _label(ALLOCATION_TARGET_LABELS, r.out.target, lang),
            18,
        ),
        xlsx.Column(
            "recipient",
            {"uz_latn": "Qabul qiluvchi nomi", "ru": "Название получателя"},
            lambda r: xlsx.localized(r.out.recipient_name, lang),
            24,
        ),
        xlsx.Column("amount", {"uz_latn": "Summa", "ru": "Сумма"}, f("amount"), 14),
        xlsx.Column(
            "account", {"uz_latn": "Bank hisobi", "ru": "Банковский счёт"}, f("account"), 20
        ),
        xlsx.Column("note", {"uz_latn": "Izoh", "ru": "Примечание"}, f("note"), 26),
        xlsx.Column("occurred_at", {"uz_latn": "Vaqt", "ru": "Время"}, f("occurred_at"), 18),
        xlsx.id_column(),
    ]


async def allocation_rows(
    db: AsyncSession,
    *,
    lang: xlsx.Lang,
    invoice_id: uuid.UUID | None,
    period_from: date | None,
    period_to: date | None,
) -> tuple[list[AllocationRow], int, int]:
    """(rows, total, cap). Calls `backoffice_service.list_allocations` +
    `allocations_out` — the exact pair `GET /payments/allocations` uses,
    including its own `ERR-VAL-001` on a missing/reversed period (ruling
    R2: the export never widens what the route would otherwise refuse)."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    rows, total = await backoffice_service.list_allocations(
        db, invoice_id=invoice_id, period_from=period_from, period_to=period_to, limit=cap, offset=0
    )
    outs = await backoffice_service.allocations_out(db, rows)
    invoice_ids = {out.invoice_id for out in outs}
    numbers = await _invoice_numbers_by_ids(db, invoice_ids)
    result = [AllocationRow(out, invoice_number=numbers.get(out.invoice_id, "")) for out in outs]
    return result, total, cap


def render_allocations(items: list[AllocationRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, allocation_columns(lang), lang=lang, title=ALLOCATIONS_TITLE[lang])


# --- Refunds (Task C.2, `GET /refunds/export.xlsx`) --------------------------
# Mirrors `adminka/src/pages/accountant/statusMeta.ts::REFUND_STATUS_LABEL_I18N`
# (`REFUND_STATUSES`), checked 2026-09-11.
REFUND_STATUS_LABELS: dict[str, dict[str, str]] = {
    "requested": {"uz_latn": "Soʻralgan", "ru": "Запрошено"},
    "in_review": {"uz_latn": "Koʻrib chiqilmoqda", "ru": "На рассмотрении"},
    "returned": {"uz_latn": "Qaytarildi", "ru": "Возвращено"},
    "rejected": {"uz_latn": "Rad etildi", "ru": "Отклонено"},
}
REFUNDS_TITLE: dict[str, str] = {"uz_latn": "Qaytarishlar", "ru": "Возвраты"}


class RefundRow:
    """One refund plus what THIS reader may see of it. `staff=False` is the
    citizen reading their own refund (stage 11, ruling R3): the accountant's
    working figure (`suggested_amount`) is blanked, and `comment` — one
    column three writers share — only while the refund is still requested,
    exactly what `refunds_router._refund_out` blanks on the screen. The file
    must never show a citizen a cell their own screen hides."""

    def __init__(
        self, refund: Refund, *, application_number: str, basis_name: str, staff: bool
    ) -> None:
        self.refund = refund
        self.id = refund.id
        self.application_number = application_number
        self.basis_name = basis_name
        self.staff = staff

    @property
    def suggested_amount(self) -> Any:
        return self.refund.suggested_amount if self.staff else None

    @property
    def comment(self) -> str | None:
        if self.staff or self.refund.status == backoffice_service._STATUS_REQUESTED:
            return self.refund.comment
        return None


def refund_columns(lang: xlsx.Lang) -> list[xlsx.Column[RefundRow]]:
    f = lambda name: lambda r: getattr(r.refund, name)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "application", {"uz_latn": "Ariza", "ru": "Заявка"}, lambda r: r.application_number, 18
        ),
        xlsx.Column("basis", {"uz_latn": "Asos", "ru": "Основание"}, lambda r: r.basis_name, 24),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(REFUND_STATUS_LABELS, r.refund.status, lang),
            20,
        ),
        xlsx.Column(
            "suggested_amount",
            {"uz_latn": "Tavsiya etilgan", "ru": "Рекомендовано"},
            lambda r: r.suggested_amount,
            16,
        ),
        xlsx.Column(
            "final_amount",
            {"uz_latn": "Yakuniy summa", "ru": "Итоговая сумма"},
            f("final_amount"),
            16,
        ),
        xlsx.Column("comment", {"uz_latn": "Izoh", "ru": "Комментарий"}, lambda r: r.comment, 26),
        xlsx.Column(
            "requested_at",
            {"uz_latn": "Soʻralgan sana", "ru": "Дата заявки"},
            f("requested_at"),
            18,
        ),
        xlsx.Column("due_at", {"uz_latn": "Muddat", "ru": "Срок"}, f("due_at"), 14),
        xlsx.Column(
            "decided_at", {"uz_latn": "Qaror sanasi", "ru": "Дата решения"}, f("decided_at"), 18
        ),
        xlsx.id_column(),
    ]


async def refund_rows(
    db: AsyncSession,
    *,
    actor: User,
    lang: xlsx.Lang,
    application_id: uuid.UUID | None,
    status: str | None,
) -> tuple[list[RefundRow], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    rows, total = await backoffice_service.list_refunds(
        db, application_id=application_id, status=status, limit=cap, offset=0, actor=actor
    )
    app_ids = {row.application_id for row in rows}
    app_numbers = await applications_service.numbers_by_ids(db, app_ids)
    if rows:
        reasons = await admin_service.classifier_items_by_code(db, REFUND_REASONS_CLASSIFIER_CODE)
        basis_names = {item.id: xlsx.localized(item.name, lang) for item in reasons}
    else:
        basis_names = {}
    staff = await service.holds_payments_read(db, actor)
    out = [
        RefundRow(
            row,
            application_number=app_numbers.get(row.application_id) or "",
            basis_name=basis_names.get(row.basis_item_id, ""),
            staff=staff,
        )
        for row in rows
    ]
    return out, total, cap


def render_refunds(items: list[RefundRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, refund_columns(lang), lang=lang, title=REFUNDS_TITLE[lang])


# --- Recipients (Task C.2, `GET /payments/recipients/export.xlsx`)
# Mirrors `adminka/src/pages/admin/recipients/labels.ts` (`kindPercent`/
# `kindFixed`, `statusActive`/`statusInactive`), checked 2026-09-11.
RECIPIENT_KIND_LABELS: dict[str, dict[str, str]] = {
    "percent": {"uz_latn": "Foiz", "ru": "Процент"},
    "fixed": {"uz_latn": "Qat'iy summa", "ru": "Фиксированная сумма"},
}
RECIPIENTS_TITLE: dict[str, str] = {
    "uz_latn": "Toʻlovlarni boʻlish — qabul qiluvchilar",
    "ru": "Разделение платежей — получатели",
}


class RecipientRow:
    def __init__(self, out: PaymentRecipientOut, *, created_by: str) -> None:
        self.out = out
        self.id = out.id
        self.created_by = created_by


def recipient_columns(lang: xlsx.Lang) -> list[xlsx.Column[RecipientRow]]:
    f = lambda name: lambda r: getattr(r.out, name)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "name",
            {"uz_latn": "Nomi", "ru": "Название"},
            lambda r: xlsx.localized(r.out.name, lang),
            26,
        ),
        xlsx.Column(
            "kind",
            {"uz_latn": "Qoidasi turi", "ru": "Тип правила"},
            lambda r: _label(RECIPIENT_KIND_LABELS, r.out.kind, lang),
            16,
        ),
        xlsx.Column("percent", {"uz_latn": "Foiz", "ru": "Процент"}, f("percent"), 10),
        xlsx.Column(
            "fixed_amount",
            {"uz_latn": "Qat'iy summa", "ru": "Фиксированная сумма"},
            f("fixed_amount"),
            16,
        ),
        xlsx.Column(
            "payme_account_id",
            {"uz_latn": "Payme hisobi", "ru": "Счёт Payme"},
            f("payme_account_id"),
            22,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _active_label(r.out.active, lang),
            12,
        ),
        xlsx.Column("sort_order", {"uz_latn": "Tartib", "ru": "Порядок"}, f("sort_order"), 10),
        xlsx.Column("note", {"uz_latn": "Izoh", "ru": "Примечание"}, f("note"), 26),
        xlsx.Column(
            "created_by", {"uz_latn": "Yaratgan", "ru": "Создал"}, lambda r: r.created_by, 22
        ),
        xlsx.Column("created_at", {"uz_latn": "Yaratilgan", "ru": "Создано"}, f("created_at"), 18),
        xlsx.id_column(),
    ]


async def recipient_rows(
    db: AsyncSession, *, lang: xlsx.Lang
) -> tuple[list[RecipientRow], int, int]:
    """(rows, total, cap). Calls `recipients_service.list_all` — the exact
    function `GET /payments/recipients` uses — with `PageParams.
    model_construct` (that model validates `page_size <= 100` on
    construction; the export is the one caller legitimately above it,
    ruling R2/R3)."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    page = await recipients_service.list_all(
        db, params=PageParams.model_construct(page=1, page_size=cap)
    )
    creator_ids = {item.created_by for item in page.items if item.created_by}
    names = await auth_service.user_names(db, creator_ids)
    rows = [
        RecipientRow(item, created_by=names.get(item.created_by, "") if item.created_by else "")
        for item in page.items
    ]
    return rows, page.total, cap


def render_recipients(items: list[RecipientRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, recipient_columns(lang), lang=lang, title=RECIPIENTS_TITLE[lang])
