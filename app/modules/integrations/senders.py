"""Destination → sender registry (plan 03.4 ruling 6). A sender takes the delivery
session and the raw payload dict, and either returns (delivered) or raises (retried
by the worker). 3.5 registers real channels; v1 has only the OTP mock wrapper (see
service module bottom)."""

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

# The session is the worker's delivery transaction: a sender that owns a business
# record (a notification) updates its status atomically with the attempt (plan
# 03.5 ruling 14). Senders that need nothing from the DB ignore it.
Sender = Callable[[AsyncSession, dict[str, Any]], Awaitable[None]]

SENDERS: dict[str, Sender] = {}


def register_sender(destination: str, sender: Sender) -> None:
    if destination in SENDERS:
        raise ValueError(f"sender already registered: {destination}")
    SENDERS[destination] = sender
