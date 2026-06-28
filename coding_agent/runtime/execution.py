"""Execution-local runtime context independent from audit event metadata."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from ..config import WORKDIR

_EXECUTION_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "execution_context",
    default=None,
)


def current_execution_context() -> dict[str, Any]:
    context = _EXECUTION_CONTEXT.get()
    return dict(context) if context else {}


def execution_workspace(fallback: str | Path | None = None) -> Path:
    workspace = current_execution_context().get("workspace")
    if workspace is not None:
        return Path(workspace).resolve()
    return Path(fallback or WORKDIR).resolve()


@contextmanager
def execution_context(
    *,
    workspace: str | Path | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    source: str | None = None,
    detached: bool | None = None,
    clear_run_id: bool = False,
) -> Iterator[None]:
    next_context = current_execution_context()
    if workspace is not None:
        next_context["workspace"] = str(Path(workspace).resolve())
    if session_id is not None:
        next_context["session_id"] = str(session_id)
    if clear_run_id:
        next_context.pop("run_id", None)
    elif run_id is not None:
        next_context["run_id"] = str(run_id)
    if source is not None:
        next_context["source"] = str(source)
    if detached is not None:
        next_context["detached"] = bool(detached)

    token = _EXECUTION_CONTEXT.set(next_context)
    try:
        yield
    finally:
        _EXECUTION_CONTEXT.reset(token)
