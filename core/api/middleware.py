"""Cross-cutting HTTP middleware.

``Correlation-Id`` is a system-wide convention from architecture/contracts.md:
nginx generates it, every call, event and audit record carries it, and the
whole chain of one action is assembled by it. A request that arrives without
the header gets one here, so nothing downstream ever has to cope with its
absence.
"""

from contextvars import ContextVar
from logging import Filter, LogRecord
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

CORRELATION_ID_HEADER = "Correlation-Id"

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    """Return the correlation id of the action being handled."""
    return _correlation_id.get()


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Read the correlation id, publish it to the context, echo it back."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or str(uuid4())
        token = _correlation_id.set(correlation_id)
        try:
            response = await call_next(request)
        finally:
            _correlation_id.reset(token)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response


class CorrelationIdFilter(Filter):
    """Add the correlation id to every log record.

    A mandatory field of every log line, see the engineering standards,
    section 7. Attach this filter to the logging configuration of the process.
    """

    def filter(self, record: LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        return True
