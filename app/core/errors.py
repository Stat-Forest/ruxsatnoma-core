"""Доменные ошибки: коды ERR-* из ЧТЗ (tz/10), единый формат ответа."""

ERRORS: dict[str, tuple[int, str]] = {
    "ERR-AUTH-001": (401, "Неверные учётные данные"),
    "ERR-AUTH-002": (401, "Сессия истекла"),
    "ERR-AUTH-003": (429, "Превышено число попыток входа, аккаунт временно заблокирован"),
    "ERR-AUTH-004": (401, "Сертификат ЭРИ истёк или отозван"),
    "ERR-AUTH-005": (403, "Верификация «Raqamli nazorat» не пройдена"),
    "ERR-AUTH-006": (403, "CSRF-токен отсутствует или неверен"),
    "ERR-AUTH-007": (403, "Требуется смена пароля"),
    "ERR-AUTH-008": (403, "Регистрация не завершена"),
    "ERR-AUTH-009": (429, "Слишком много запросов кода подтверждения"),
    "ERR-AUTH-010": (400, "Неверный или истёкший код подтверждения"),
    "ERR-AUTH-011": (409, "Представительство уже действует"),
    "ERR-AUTH-012": (409, "Регистрация уже завершена"),
    "ERR-ACL-001": (403, "Нет прав на ресурс"),
    "ERR-ACL-002": (403, "Запрос вне территориальной зоны"),
    "ERR-ACL-003": (403, "Изменение запрещено для роли только-чтение"),
    "ERR-APP-001": (400, "Не заполнено обязательное поле"),
    "ERR-APP-002": (409, "Активная заявка на пересекающийся период уже существует"),
    "ERR-APP-003": (422, "Неполный комплект документов"),
    "ERR-APP-004": (409, "Недопустимый переход статуса заявки"),
    "ERR-GIS-001": (422, "Невалидная геометрия"),
    "ERR-GIS-002": (422, "Геометрия вне границ лесного фонда"),
    "ERR-GIS-003": (422, "Пересечение со слоем ограничений или охраны"),
    "ERR-GIS-004": (422, "Ошибка формата файла импорта"),
    "ERR-GIS-005": (409, "Конфликт состояния GIS-объекта"),
    # `gis.service.split_contour` (decision #91): the two client-supplied
    # pieces, normalised the same way `insert_version` normalises any
    # geometry, do not reconstruct the parent's own published boundary — a
    # gap, an overlap beyond the module's own tolerance, or a piece that
    # collapses to nothing. Never `ERR-GIS-005`: this is a defect in the
    # SUBMITTED GEOMETRY itself, not a conflict with the parent's STATE.
    "ERR-GIS-006": (422, "Части не образуют точное разделение родительского контура"),
    "ERR-NORM-001": (422, "На контуре нет утверждённой нормы"),
    "ERR-NORM-002": (422, "Превышен остаток лимита"),
    "ERR-NORM-003": (422, "Период не соответствует сезону или ротации"),
    "ERR-NORM-004": (422, "Не задан параметр расчёта"),
    "ERR-NORM-005": (409, "Конфликт состояния или периода нормы"),
    "ERR-NORM-006": (422, "Действует пожарный запрет"),
    "ERR-PAY-001": (422, "Подтверждение оплаты не поступило"),
    "ERR-PAY-002": (422, "Срок оплаты истёк"),
    "ERR-PAY-003": (422, "Сумма оплаты не совпадает с инвойсом"),
    "ERR-PAY-004": (409, "Инвойс не может быть оплачен в текущем статусе"),
    # Registered by 3.10b task 1, raised by nobody yet — reserved for Task 5
    # of this stage (closing a reconciliations row), the same "registered,
    # not dead" pattern as ERR-PERM-002.
    "ERR-PAY-005": (409, "Расхождение уже закрыто"),
    # 3.10b task 9: a refund is not in the status `submit-decision`/`approve`
    # each require — already decided, decided by someone else while this
    # request waited on the row lock, or `approve` called before
    # `submit-decision` ever ran. Never `ERR-PAY-004`, whose registered
    # message names an invoice specifically (the same reasoning
    # `backoffice_service.resolve_reconciliation` gives for minting its own
    # `ERR-PAY-005` rather than reusing that code for a reconciliation).
    "ERR-PAY-006": (409, "Запрос на возврат не может быть изменён в текущем статусе"),
    # Stage 7.9 task 6, decision #160: the frozen split (`invoice_recipients`)
    # names a receiver with no `payme_account_id` — the citizen's own
    # `POST /invoices/{id}/pay-intents` button refuses BEFORE a checkout link
    # is even built, `details.missing` naming the receivers (position, name).
    # Fail-closed by design: a split we cannot ROUTE at Payme is refused
    # rather than taken onto the Agency's cashbox for someone to move by
    # hand. The two Payme RPC methods (`CheckPerformTransaction`,
    # `CreateTransaction`) answer the SAME condition as `-31008` instead —
    # they are outside the `ERR-*` envelope entirely (`payme.py`'s own
    # module docstring), never this code.
    "ERR-PAY-007": (409, "Разделение платежа не может быть маршрутизировано"),
    "ERR-PERM-001": (409, "Недопустимый переход статуса разрешения"),
    "ERR-PERM-002": (409, "Документ уже подписан и не может быть перевыпущен"),
    "ERR-PERM-003": (409, "Конфликт состояния лесного билета"),
    "ERR-REP-001": (409, "Конфликт состояния отчёта"),
    "ERR-REP-002": (422, "Отчёт не проходит логические проверки"),
    "ERR-REP-003": (422, "Форма отчёта не может быть использована"),
    "ERR-SIGN-001": (422, "Ошибка подписания"),
    "ERR-SIGN-002": (409, "Эта подпись уже проставлена"),
    "ERR-SIGN-003": (422, "Не хватает подписей"),
    "ERR-SIGN-004": (409, "Конфликт состояния сертификата или подписи"),
    # 4.1 inspections: task/act/case state-conflict (a bad transition, or an
    # action against a row already past the point it applies to).
    "ERR-INSP-001": (409, "Недопустимый переход статуса инспекции"),
    # A checklist answer set that does not satisfy the checklist's own
    # required questions.
    "ERR-INSP-002": (422, "Чек-лист заполнен не полностью"),
    "ERR-INT-001": (503, "Внешний сервис не ответил"),
    "ERR-INT-002": (502, "Внешний сервис вернул ошибку"),
    "ERR-SYS-001": (500, "Внутренняя ошибка сервера"),
    "ERR-SYS-002": (503, "Сервис временно недоступен"),
    "ERR-SYS-003": (404, "Ресурс не найден"),
    "ERR-SYS-004": (405, "Метод не поддерживается"),
    "ERR-SYS-005": (409, "Конфликт Idempotency-Key"),
    "ERR-SYS-006": (429, "Слишком много запросов"),
    "ERR-VAL-001": (422, "Ошибка валидации входных данных"),
    # 4.6 `public`: a citizen appeal cannot make the requested status transition.
    "ERR-PUB-001": (409, "Недопустимый переход статуса обращения"),
    # 4.8 `help`: a support ticket cannot make the requested transition, or
    # cannot accept a new message, in its current status (closed).
    "ERR-HELP-001": (409, "Недопустимое действие для текущего статуса обращения в поддержку"),
    # Track B4 (4.5 search, 4.7 archive), plan `04.5-4.7-search-archive.md`.
    "ERR-SRCH-001": (409, "Профиль поиска с таким именем уже существует"),
    "ERR-ARCH-001": (409, "Объект не может быть архивирован в текущем статусе"),
    "ERR-ARCH-002": (409, "Нарушена целостность архивной копии"),
}


class DomainError(Exception):
    def __init__(self, code: str, http_status: int, message: str, details: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.http_status = http_status
        self.message = message
        self.details = details


def err(code: str, details: dict | None = None, message: str | None = None) -> DomainError:
    if code not in ERRORS:
        raise KeyError(f"Неизвестный код ошибки: {code}")
    http_status, default_message = ERRORS[code]
    return DomainError(code, http_status, message or default_message, details)
