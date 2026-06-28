"""Cooperative cancellation state for one agent run."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


class RunCancelled(Exception):
    """Raised at a safe runtime boundary after cancellation is requested."""


_CANCEL_EVENT: ContextVar[threading.Event | None] = ContextVar(
    "run_cancel_event",
    default=None,
)


@contextmanager
def cancellation_context(event: threading.Event) -> Iterator[None]:
    token = _CANCEL_EVENT.set(event)
    try:
        yield
    finally:
        _CANCEL_EVENT.reset(token)


def cancellation_requested() -> bool:
    event = _CANCEL_EVENT.get()
    return bool(event and event.is_set())


def raise_if_cancelled() -> None:
    if cancellation_requested():
        raise RunCancelled("Run cancelled by user.")
