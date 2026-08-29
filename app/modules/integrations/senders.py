"""Destination → sender registry (plan 03.4 ruling 6). A sender takes the raw
payload dict and either returns (delivered) or raises (retried by the worker).
3.5 registers real channels; v1 has only the OTP mock wrapper (see service
module bottom)."""

from collections.abc import Awaitable, Callable
from typing import Any

Sender = Callable[[dict[str, Any]], Awaitable[None]]

SENDERS: dict[str, Sender] = {}


def register_sender(destination: str, sender: Sender) -> None:
    if destination in SENDERS:
        raise ValueError(f"sender already registered: {destination}")
    SENDERS[destination] = sender
