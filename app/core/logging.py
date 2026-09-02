"""Структурированные логи: structlog; console в dev, JSON в prod."""

import logging
from typing import Literal

import structlog

# structlog contextvars key: the correlation middleware binds it, audit.service reads it.
CORRELATION_ID_KEY = "correlation_id"

# Paths whose uvicorn access-log line must never be written (3.11a t5, review I2).
#
# `qr_check_log` records no IP address and no personal data, by `design/02`'s
# explicit instruction — the page it counts is unauthenticated, so anything
# identifying stored beside a permit id would be a record of who looked at whose
# permit. uvicorn's access log undoes that from outside the application: every
# check would write `IP — GET /api/v1/public/permits/check?qr=<token> 200` at
# INFO, which is both the visitor's address and the printed token, in plaintext,
# in a file no purge job covers. The token in the URL is inherent (a phone camera
# opens a URL, and ruling 8 says the token's secrecy is not the control); the IP
# is the part that contradicts what this module states about itself.
#
# Dropping the line loses nothing: the module keeps its OWN count of every call
# in `qr_check_log`, deliberately without the identifying half.
SILENT_ACCESS_LOG_PATHS = ("/api/v1/public/permits/check",)


class _SilentPathFilter(logging.Filter):
    """Drops uvicorn access-log records for `SILENT_ACCESS_LOG_PATHS`.

    uvicorn logs with `args = (client_addr, method, full_path, http_version,
    status)`, so the path is read from the record's own arguments rather than
    from a rendered message. The formatted message is checked as a fallback, in
    case a deployment swaps the access formatter for one of its own.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        candidates: list[str] = []
        if isinstance(record.args, tuple):
            candidates.extend(str(arg) for arg in record.args)
        else:
            candidates.append(record.getMessage())
        return not any(
            candidate.startswith(path) or f" {path}" in candidate
            for candidate in candidates
            for path in SILENT_ACCESS_LOG_PATHS
        )


def silence_access_log_for_public_paths() -> None:
    """Install `_SilentPathFilter` on uvicorn's access logger, once.

    Called from `configure_logging`, which `create_app()` runs — so it applies
    however the app is served, including `uvicorn app.main:create_app --factory`
    where uvicorn has already configured its own logging by the time the app is
    imported (`logging.getLogger` returns that same logger object).
    """
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, _SilentPathFilter) for existing in logger.filters):
        logger.addFilter(_SilentPathFilter())


def configure_logging(log_format: Literal["console", "json"]) -> None:
    silence_access_log_for_public_paths()
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if log_format == "json":
        # dict_tracebacks — иначе JSONRenderer теряет traceback исключения (не умеет
        # сериализовать exc_info как есть); console-ветка рендерит exc_info сама.
        processors.append(structlog.processors.dict_tracebacks)
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
