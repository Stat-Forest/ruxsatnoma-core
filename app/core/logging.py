"""Структурированные логи: structlog; console в dev, JSON в prod."""

import logging
from typing import Literal

import structlog


def configure_logging(log_format: Literal["console", "json"]) -> None:
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
