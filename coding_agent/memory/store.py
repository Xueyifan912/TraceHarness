"""Workspace-local long-term memory store."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..config import WORKDIR
from ..runtime.execution import execution_workspace
from ..runtime.events import log_event
from ..runtime.fileio import exclusive_file_lock, safe_runtime_path

MEMORY_DIR_NAME = ".memory"
MEMORY_FILE_NAME = "MEMORY.md"
INJECTION_LIMIT = 2000
READ_PREVIEW_LIMIT = 4000
APPEND_MAX_LENGTH = 20 * 1024


def memory_path(workspace: str | Path | None = None) -> Path:
    root = execution_workspace(WORKDIR) if workspace is None else Path(workspace)
    return safe_runtime_path(root, MEMORY_DIR_NAME, MEMORY_FILE_NAME)


def default_memory_path() -> Path:
    return memory_path(WORKDIR)


def read_memory_raw(workspace: str | Path | None = None) -> str:
    try:
        path = memory_path(workspace)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def memory_injection_text(workspace: str | Path | None = None) -> str:
    return read_memory_raw(workspace)[:INJECTION_LIMIT]


def _preview(text: str, limit: int = READ_PREVIEW_LIMIT) -> dict[str, Any]:
    return {
        "preview": text[:limit],
        "length": len(text),
        "truncated": len(text) > limit,
    }


def _append_audit_payload(path: Path, text: str) -> dict[str, Any]:
    return {
        "memory_path": str(path),
        "content_length": len(text),
        "content_omitted": True,
    }


def read_memory_for_tool(workspace: str | Path | None = None) -> str:
    root = execution_workspace(WORKDIR) if workspace is None else Path(workspace)
    text = read_memory_raw(root)
    log_event("memory_read", {
        "memory_path": str(memory_path(root)),
        "length": len(text),
    }, workspace=root)
    if not text.strip():
        return "(memory empty)"
    if len(text) <= READ_PREVIEW_LIMIT:
        return text
    payload = _preview(text)
    return (
        f"[Memory preview: {payload['length']} chars, truncated]\n"
        f"{payload['preview']}"
    )


def append_memory(content: str, workspace: str | Path | None = None) -> str:
    text = str(content or "").strip()
    if not text:
        return "Error: memory content is empty"
    if len(text) > APPEND_MAX_LENGTH:
        return f"Error: memory content exceeds {APPEND_MAX_LENGTH} chars"
    root = execution_workspace(WORKDIR) if workspace is None else Path(workspace)
    path = memory_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(path.with_name(f".{path.name}.lock")):
            prefix = ""
            if path.exists() and path.read_text(encoding="utf-8").strip():
                prefix = "\n"
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(f"{prefix}- {text}\n")
                handle.flush()
                os.fsync(handle.fileno())
    except Exception as e:
        return f"Error: {e}"
    log_event("memory_append", _append_audit_payload(path, text),
              workspace=root)
    return f"Appended memory ({len(text)} chars)"
