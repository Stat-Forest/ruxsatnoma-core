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
mandatory LAST column on all three (ruling R4). `permit_id` is printed as
a raw id on every sheet that carries one: `permits.service` has no batch
name reader (`grep -n "def .*_by_ids" app/modules/permits/service.py`
finds none), and adding one there is out of this track's scope."""

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
from app.modules.inspections.models import InspectionTask

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
TASKS_TITLE = {"uz_latn": "Topshiriqlar", "ru": "Задания"}


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
    ) -> None:
        self.task = task
        self.id = task.id
        self.inspector = inspector
        self.organization = organization
        self.application_number = application_number
        self.contour_number = contour_number


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
            "permit_id",
            {"uz_latn": "Ruxsatnoma ID", "ru": "ID разрешения"},
            lambda r: str(r.task.permit_id) if r.task.permit_id else "",
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
    return (
        [
            TaskRow(
                task,
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
