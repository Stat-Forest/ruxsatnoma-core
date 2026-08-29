"""Per-destination circuit breaker for outbox delivery (plan 03.5 ruling 13).

In-process state, like app/core/ratelimit.py: N worker processes mean N breakers.
That is acceptable — the breaker is a courtesy to a struggling provider and a way
to stop burning `attempts` during an outage, not a distributed guarantee."""

import time
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class _State:
    failures: int = 0
    open_until: float = field(default=0.0)


_state: dict[str, _State] = {}


def reset() -> None:
    """Tests only."""
    _state.clear()


def open_destinations() -> list[str]:
    now = time.monotonic()
    return [destination for destination, s in _state.items() if s.open_until > now]


def record_success(destination: str) -> None:
    _state.pop(destination, None)


def record_failure(destination: str, *, threshold: int, cooldown_seconds: int) -> bool:
    """Count one failure; returns True if this one tripped the breaker."""
    state = _state.setdefault(destination, _State())
    state.failures += 1
    if state.failures < max(1, threshold):
        return False
    state.failures = 0
    state.open_until = time.monotonic() + cooldown_seconds
    logger.warning("outbox.breaker_open", destination=destination, cooldown=cooldown_seconds)
    return cooldown_seconds > 0
