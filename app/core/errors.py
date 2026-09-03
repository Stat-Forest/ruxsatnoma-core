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
    "ERR-PERM-001": (409, "Недопустимый переход статуса разрешения"),
    "ERR-PERM-002": (409, "Документ уже подписан и не может быть перевыпущен"),
    "ERR-SIGN-001": (422, "Ошибка подписания"),
    "ERR-SIGN-002": (409, "Эта подпись уже проставлена"),
    "ERR-SIGN-003": (422, "Не хватает подписей"),
    "ERR-SIGN-004": (409, "Конфликт состояния сертификата или подписи"),
    "ERR-INT-001": (503, "Внешний сервис не ответил"),
    "ERR-INT-002": (502, "Внешний сервис вернул ошибку"),
    "ERR-SYS-001": (500, "Внутренняя ошибка сервера"),
    "ERR-SYS-002": (503, "Сервис временно недоступен"),
    "ERR-SYS-003": (404, "Ресурс не найден"),
    "ERR-SYS-004": (405, "Метод не поддерживается"),
    "ERR-SYS-005": (409, "Конфликт Idempotency-Key"),
    "ERR-SYS-006": (429, "Слишком много запросов"),
    "ERR-VAL-001": (422, "Ошибка валидации входных данных"),
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
