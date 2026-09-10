"""`GET /inspections/{tasks,acts,cases}/export.xlsx` (stage 13, ruling #204):
each list on paper. Every `rows()` here calls the SAME service function its
sibling list route calls (ruling R2) — the same scope, the same filters —
then resolves every id the sheet shows to a name in ONE query per table.
Column headers and status/result/decision labels are copied from the
adminka's own dictionaries (`src/i18n/*`, `src/pages/inspector/*`) so the
file reads like the screen.

Neither `InspectionTask` nor `InspectionAct` carries a human-readable
number (unlike `ViolationCase.number`) — the screen itself identifies a
task/act only by its kind/date and the `ID` column, so there is no "number
first" column for those two sheets; `xlsx.id_column()` is still the
mandatory LAST column on all three (ruling R4). The permit a row names is
printed by its display number through `permits.service.permit_numbers_by_ids`
— one batch per sheet, the same formatter the document itself prints."""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.admin import service as admin_service
from app.modules.applications import service as applications_service
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.inspections import service
from app.modules.inspections.models import InspectionAct, InspectionTask, ViolationCase
from app.modules.permits import service as permits_service

TASK_KIND_LABELS: dict[str, dict[str, str]] = {
    "pre_approval_visit": {"uz_latn": "Berishdan oldingi tashrif", "ru": "Выезд перед выдачей"},
    "permit_inspection": {"uz_latn": "Ruxsatnomani tekshirish", "ru": "Проверка разрешения"},
}
TASK_STATUS_LABELS: dict[str, dict[str, str]] = {
    "assigned": {"uz_latn": "Tayinlangan", "ru": "Назначено"},
    "in_progress": {"uz_latn": "Jarayonda", "ru": "В процессе"},
    "done": {"uz_latn": "Bajarildi", "ru": "Выполнено"},
    "cancelled": {"uz_latn": "Bekor qilindi", "ru": "Отменено"},
}
ACT_STATUS_LABELS: dict[str, dict[str, str]] = {
    "draft": {"uz_latn": "Qoralama", "ru": "Черновик"},
    "signed": {"uz_latn": "Imzolangan", "ru": "Подписан"},
}
ACT_RESULT_LABELS: dict[str, dict[str, str]] = {
    "compliant": {"uz_latn": "Mos", "ru": "Соответствует"},
    "warning": {"uz_latn": "Eslatma", "ru": "Замечание"},
    "violation": {"uz_latn": "Buzilish", "ru": "Нарушение"},
}
CASE_STATUS_LABELS: dict[str, dict[str, str]] = {
    "opened": {"uz_latn": "Ochilgan", "ru": "Открыто"},
    "explanation_requested": {"uz_latn": "Tushuntirish soʻralgan", "ru": "Запрошено объяснение"},
    "explained": {"uz_latn": "Tushuntirish berilgan", "ru": "Объяснение получено"},
    "decided": {"uz_latn": "Qaror qabul qilingan", "ru": "Решение принято"},
    "appealed": {"uz_latn": "Shikoyat qilingan", "ru": "Обжаловано"},
    "closed": {"uz_latn": "Yopilgan", "ru": "Закрыто"},
    "archived": {"uz_latn": "Arxivda", "ru": "В архиве"},
}
CASE_DECISION_LABELS: dict[str, dict[str, str]] = {
    "warning": {"uz_latn": "Ogohlantirish", "ru": "Предупреждение"},
    "suspend": {"uz_latn": "Ruxsatnomani toʻxtatish", "ru": "Приостановка разрешения"},
    "revoke": {"uz_latn": "Ruxsatnomani bekor qilish", "ru": "Аннулирование разрешения"},
    "transfer": {"uz_latn": "Boshqa organga oʻtkazish", "ru": "Передача в другой орган"},
}
TASKS_TITLE = {"uz_latn": "Topshiriqlar", "ru": "Задания"}
ACTS_TITLE = {"uz_latn": "Tekshiruv aktlari", "ru": "Акты проверок"}
CASES_TITLE = {"uz_latn": "Buzilish ishlari", "ru": "Дела о нарушениях"}


def _label(table: dict[str, dict[str, str]], code: str | None, lang: xlsx.Lang) -> str:
    """An unknown or missing code renders as itself (or empty when `code` is
    `None`) — never a blank cell hiding a real, just-unrecognised value."""
    if code is None:
        return ""
    return table.get(code, {}).get(lang, code)


# --- Tasks -----------------------------------------------------------------


class TaskRow:
    """One `InspectionTask` plus the names its ids resolve to — columns read
    attributes off this, so every resolver runs once per table, not per
    row."""

    def __init__(
        self,
        task: InspectionTask,
        *,
        inspector: str,
        organization: str,
        application_number: str,
        contour_number: str,
        permit_number: str,
    ) -> None:
        self.task = task
        self.id = task.id
        self.inspector = inspector
        self.organization = organization
        self.application_number = application_number
        self.contour_number = contour_number
        self.permit_number = permit_number


def task_columns(lang: xlsx.Lang) -> list[xlsx.Column[TaskRow]]:
    a = lambda f: lambda r: getattr(r.task, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "kind",
            {"uz_latn": "Turi", "ru": "Вид"},
            lambda r: _label(TASK_KIND_LABELS, r.task.kind, lang),
            22,
        ),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(TASK_STATUS_LABELS, r.task.status, lang),
            16,
        ),
        xlsx.Column("due_at", {"uz_latn": "Muddat", "ru": "Срок"}, a("due_at"), 14),
        xlsx.Column(
            "application",
            {"uz_latn": "Ariza raqami", "ru": "Номер заявки"},
            lambda r: r.application_number,
            20,
        ),
        xlsx.Column(
            "contour", {"uz_latn": "Kontur", "ru": "Контур"}, lambda r: r.contour_number, 14
        ),
        xlsx.Column(
            "permit",
            {"uz_latn": "Ruxsatnoma", "ru": "Разрешение"},
            lambda r: r.permit_number,
            16,
        ),
        xlsx.Column(
            "inspector", {"uz_latn": "Inspektor", "ru": "Инспектор"}, lambda r: r.inspector, 26
        ),
        xlsx.Column(
            "organization",
            {"uz_latn": "Oʻrmon xoʻjaligi", "ru": "Лесхоз"},
            lambda r: r.organization,
            30,
        ),
        xlsx.Column("created_at", {"uz_latn": "Yaratilgan", "ru": "Создан"}, a("created_at"), 18),
        xlsx.id_column(),
    ]


async def task_rows(
    db: AsyncSession, *, actor: User, lang: xlsx.Lang, status: str | None
) -> tuple[list[TaskRow], int, int]:
    """(rows, total, cap). `model_construct` bypasses `PageParams`'s own
    `page_size <= 100` — the export is the one caller legitimately above it,
    the cap is what bounds it instead (ruling R3)."""
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    tasks, total = await service.list_tasks(
        db, status=status, params=PageParams.model_construct(page=1, page_size=cap), actor=actor
    )
    inspectors = await auth_service.user_names(db, {t.assigned_to for t in tasks})
    orgs = await admin_service.organization_names(
        db, {t.organization_id for t in tasks if t.organization_id}
    )
    applications = await applications_service.numbers_by_ids(
        db, {t.application_id for t in tasks if t.application_id}
    )
    contours = await gis_service.contour_numbers_by_ids(
        db, {t.contour_id for t in tasks if t.contour_id}
    )
    permits = await permits_service.permit_numbers_by_ids(
        db, {t.permit_id for t in tasks if t.permit_id}
    )
    return (
        [
            TaskRow(
                task,
                permit_number=permits.get(task.permit_id, "") if task.permit_id else "",
                inspector=inspectors.get(task.assigned_to, ""),
                organization=xlsx.localized(orgs.get(task.organization_id), lang)
                if task.organization_id
                else "",
                application_number=(applications.get(task.application_id) or "")
                if task.application_id
                else "",
                contour_number=contours.get(task.contour_id, "") if task.contour_id else "",
            )
            for task in tasks
        ],
        total,
        cap,
    )


def render_tasks(items: Sequence[TaskRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, task_columns(lang), lang=lang, title=TASKS_TITLE[lang])


# --- Acts --------------------------------------------------------------


class ActRow:
    def __init__(
        self,
        act: InspectionAct,
        *,
        inspector: str,
        organization: str,
        application_number: str,
        permit_number: str,
    ) -> None:
        self.act = act
        self.id = act.id
        self.inspector = inspector
        self.organization = organization
        self.application_number = application_number
        self.permit_number = permit_number


def act_columns(lang: xlsx.Lang) -> list[xlsx.Column[ActRow]]:
    a = lambda f: lambda r: getattr(r.act, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column("occurred_at", {"uz_latn": "Sana", "ru": "Дата"}, a("occurred_at"), 18),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(ACT_STATUS_LABELS, r.act.status, lang),
            14,
        ),
        xlsx.Column(
            "result",
            {"uz_latn": "Natija", "ru": "Результат"},
            lambda r: _label(ACT_RESULT_LABELS, r.act.result, lang),
            16,
        ),
        xlsx.Column(
            "application",
            {"uz_latn": "Ariza raqami", "ru": "Номер заявки"},
            lambda r: r.application_number,
            20,
        ),
        xlsx.Column(
            "permit",
            {"uz_latn": "Ruxsatnoma", "ru": "Разрешение"},
            lambda r: r.permit_number,
            16,
        ),
        xlsx.Column(
            "task_id",
            {"uz_latn": "Topshiriq ID", "ru": "ID задания"},
            lambda r: str(r.act.task_id) if r.act.task_id else "",
            38,
        ),
        xlsx.Column(
            "inspector", {"uz_latn": "Inspektor", "ru": "Инспектор"}, lambda r: r.inspector, 26
        ),
        xlsx.Column(
            "organization",
            {"uz_latn": "Oʻrmon xoʻjaligi", "ru": "Лесхоз"},
            lambda r: r.organization,
            30,
        ),
        xlsx.Column("created_at", {"uz_latn": "Yaratilgan", "ru": "Создан"}, a("created_at"), 18),
        xlsx.id_column(),
    ]


async def act_rows(
    db: AsyncSession, *, actor: User, lang: xlsx.Lang, result: str | None
) -> tuple[list[ActRow], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    acts, total = await service.list_acts(
        db, result=result, params=PageParams.model_construct(page=1, page_size=cap), actor=actor
    )
    inspectors = await auth_service.user_names(db, {a.inspector_id for a in acts})
    orgs = await admin_service.organization_names(
        db, {a.organization_id for a in acts if a.organization_id}
    )
    applications = await applications_service.numbers_by_ids(
        db, {a.application_id for a in acts if a.application_id}
    )
    permits = await permits_service.permit_numbers_by_ids(
        db, {a.permit_id for a in acts if a.permit_id}
    )
    return (
        [
            ActRow(
                act,
                permit_number=permits.get(act.permit_id, "") if act.permit_id else "",
                inspector=inspectors.get(act.inspector_id, ""),
                organization=xlsx.localized(orgs.get(act.organization_id), lang)
                if act.organization_id
                else "",
                application_number=(applications.get(act.application_id) or "")
                if act.application_id
                else "",
            )
            for act in acts
        ],
        total,
        cap,
    )


def render_acts(items: Sequence[ActRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, act_columns(lang), lang=lang, title=ACTS_TITLE[lang])


# --- Cases ---------------------------------------------------------------


class CaseRow:
    def __init__(
        self, case: ViolationCase, *, applicant: str, organization: str, permit_number: str
    ) -> None:
        self.case = case
        self.id = case.id
        self.applicant = applicant
        self.organization = organization
        self.permit_number = permit_number


def case_columns(lang: xlsx.Lang) -> list[xlsx.Column[CaseRow]]:
    a = lambda f: lambda r: getattr(r.case, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column("number", {"uz_latn": "Ish raqami", "ru": "Номер дела"}, a("number"), 18),
        xlsx.Column(
            "status",
            {"uz_latn": "Holati", "ru": "Статус"},
            lambda r: _label(CASE_STATUS_LABELS, r.case.status, lang),
            20,
        ),
        xlsx.Column(
            "decision",
            {"uz_latn": "Qaror", "ru": "Решение"},
            lambda r: _label(CASE_DECISION_LABELS, r.case.decision, lang),
            22,
        ),
        xlsx.Column(
            "applicant", {"uz_latn": "Buzuvchi", "ru": "Нарушитель"}, lambda r: r.applicant, 30
        ),
        xlsx.Column(
            "organization",
            {"uz_latn": "Oʻrmon xoʻjaligi", "ru": "Лесхоз"},
            lambda r: r.organization,
            30,
        ),
        xlsx.Column(
            "permit",
            {"uz_latn": "Ruxsatnoma", "ru": "Разрешение"},
            lambda r: r.permit_number,
            16,
        ),
        xlsx.Column(
            "damage_amount",
            {"uz_latn": "Zarar summasi", "ru": "Сумма ущерба"},
            a("damage_amount"),
            16,
        ),
        xlsx.Column(
            "explanation_due_at",
            {"uz_latn": "Tushuntirish muddati", "ru": "Срок объяснения"},
            a("explanation_due_at"),
            18,
        ),
        xlsx.Column(
            "decision_due_at",
            {"uz_latn": "Qaror muddati", "ru": "Срок решения"},
            a("decision_due_at"),
            16,
        ),
        xlsx.Column(
            "decided_at", {"uz_latn": "Qaror sanasi", "ru": "Дата решения"}, a("decided_at"), 18
        ),
        xlsx.Column(
            "created_at", {"uz_latn": "Ochilgan sana", "ru": "Дата открытия"}, a("created_at"), 18
        ),
        xlsx.id_column(),
    ]


async def case_rows(
    db: AsyncSession,
    *,
    actor: User,
    lang: xlsx.Lang,
    status: str | None,
    applicant_id: uuid.UUID | None,
) -> tuple[list[CaseRow], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    cases, total = await service.list_cases(
        db,
        status=status,
        applicant_id=applicant_id,
        params=PageParams.model_construct(page=1, page_size=cap),
        actor=actor,
    )
    applicants = await auth_service.applicant_names(
        db, {c.applicant_id for c in cases if c.applicant_id}
    )
    orgs = await admin_service.organization_names(
        db, {c.organization_id for c in cases if c.organization_id}
    )
    permits = await permits_service.permit_numbers_by_ids(
        db, {c.permit_id for c in cases if c.permit_id}
    )
    return (
        [
            CaseRow(
                case,
                permit_number=permits.get(case.permit_id, "") if case.permit_id else "",
                applicant=applicants.get(case.applicant_id, "") if case.applicant_id else "",
                organization=xlsx.localized(orgs.get(case.organization_id), lang)
                if case.organization_id
                else "",
            )
            for case in cases
        ],
        total,
        cap,
    )


def render_cases(items: Sequence[CaseRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, case_columns(lang), lang=lang, title=CASES_TITLE[lang])
