"""Workspace-local session snapshots for CLI runs."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .events import log_event, scrub_sensitive_text
from .fileio import atomic_write_text, exclusive_file_lock, safe_runtime_path

SESSION_DIR = ".agent_sessions"
SESSION_ARCHIVE_DIR = "archive"
SNAPSHOT_VERSION = 1
_PROMPT_PREVIEW_LIMIT = 500
_TOOL_RESULT_PREVIEW_LIMIT = 2000
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    created_at: str
    workspace_path: str
    path: Path


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _workspace(workspace: str | Path | None = None) -> Path:
    return (Path.cwd() if workspace is None else Path(workspace)).resolve()


def session_dir(workspace: str | Path | None = None) -> Path:
    return safe_runtime_path(
        _workspace(workspace),
        SESSION_DIR,
        create_directory=True,
    )


def _is_safe_session_id(session_id: str) -> bool:
    if not isinstance(session_id, str) or not session_id:
        return False
    if "/" in session_id or "\\" in session_id:
        return False
    if Path(session_id).is_absolute():
        return False
    if set(session_id) == {"."}:
        return False
    return bool(_SESSION_ID_RE.fullmatch(session_id))


def _session_path(session_id: str, workspace: str | Path | None = None) -> Path:
    if not _is_safe_session_id(session_id):
        raise ValueError(f"Invalid session id: {session_id!r}")
    base = session_dir(workspace).resolve()
    path = (base / f"{session_id}.json").resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Session path escapes session directory: {session_id!r}")
    return path


def session_file_path(
    session_id: str,
    workspace: str | Path | None = None,
) -> Path:
    return _session_path(session_id, workspace)


def archived_session_file_path(
    session_id: str,
    workspace: str | Path | None = None,
) -> Path:
    if not _is_safe_session_id(session_id):
        raise ValueError(f"Invalid session id: {session_id!r}")
    archive = safe_runtime_path(
        _workspace(workspace),
        SESSION_DIR,
        SESSION_ARCHIVE_DIR,
        create_directory=True,
    )
    path = (archive / f"{session_id}.json").resolve()
    if not path.is_relative_to(archive):
        raise ValueError(f"Archive path escapes session directory: {session_id!r}")
    return path


def _preview_text(value: Any, limit: int = _PROMPT_PREVIEW_LIMIT) -> dict[str, Any]:
    original = str(value)
    text = scrub_sensitive_text(original)
    return {
        "preview": text[:limit],
        "length": len(original),
        "truncated": len(original) > limit,
    }


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _legacy_tool_result_preview(content: dict[str, Any]) -> str | None:
    if not {"preview", "length", "truncated"}.issubset(content):
        return None
    preview = str(content.get("preview", ""))
    length = content.get("length")
    return (
        "[Tool result truncated in saved session; "
        f"original length: {length} chars]\n{preview}"
    )


def _tool_result_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        legacy_preview = _legacy_tool_result_preview(content)
        if legacy_preview is not None:
            return legacy_preview
        return _json_text(content)
    if isinstance(content, (list, tuple)):
        return _json_text(content)
    return str(content)


def _tool_result_content_snapshot(content: Any) -> str:
    text = _tool_result_content_text(content)
    if len(text) <= _TOOL_RESULT_PREVIEW_LIMIT:
        return text
    return (
        "[Tool result truncated in saved session; "
        f"original length: {len(text)} chars]\n"
        f"{text[:_TOOL_RESULT_PREVIEW_LIMIT]}"
    )


def _serializable_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _serializable_value(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable_value(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return _serializable_value(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return _serializable_value(vars(value))
    return str(value)


def _snapshot_block(block: Any) -> Any:
    data = _serializable_value(block)
    if isinstance(data, dict) and data.get("type") == "tool_result":
        data = dict(data)
        data["content"] = _tool_result_content_snapshot(data.get("content", ""))
    return data


def _snapshot_content(content: Any) -> Any:
    if isinstance(content, list):
        return [_snapshot_block(block) for block in content]
    return _serializable_value(content)


def _snapshot_messages(messages: list[dict]) -> list[dict[str, Any]]:
    snapshot = []
    for message in messages:
        if not isinstance(message, dict):
            snapshot.append({"role": "unknown", "content": _serializable_value(message)})
            continue
        item = {str(key): _serializable_value(value)
                for key, value in message.items()
                if key != "content"}
        item["content"] = _snapshot_content(message.get("content"))
        snapshot.append(item)
    return snapshot


def _normalize_loaded_session(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    normalized = dict(data)
    messages = normalized.get("messages")
    if isinstance(messages, list):
        normalized["messages"] = _snapshot_messages(messages)
    display_messages = normalized.get("display_messages")
    if isinstance(display_messages, list):
        normalized["display_messages"] = _snapshot_messages(display_messages)
    return normalized


def _last_user_prompt(messages: list[dict]) -> str:
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if text:
                        text_parts.append(str(text))
            return "\n".join(text_parts)
        return str(content)
    return ""


def _write_json(path: Path, payload: dict[str, Any]) -> bool:
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        with exclusive_file_lock(path.with_name(f".{path.name}.lock")):
            atomic_write_text(path, text)
        return True
    except Exception:
        return False


def create_session(workspace: str | Path | None = None) -> SessionRecord:
    root = _workspace(workspace)
    created_at = _utc_timestamp()
    session_id = f"{created_at.replace(':', '').replace('.', '-')}-{uuid4().hex[:8]}"
    record = SessionRecord(
        session_id=session_id,
        created_at=created_at,
        workspace_path=str(root),
        path=_session_path(session_id, root),
    )
    payload = {
        "snapshot_version": SNAPSHOT_VERSION,
        "session_id": record.session_id,
        "created_at": record.created_at,
        "updated_at": record.created_at,
        "workspace_path": record.workspace_path,
        "message_count": 0,
        "display_message_count": 0,
        "last_user_prompt_preview": None,
        "messages": [],
        "display_messages": [],
    }
    _write_json(record.path, payload)
    log_event("session_created", {
        "session_id": record.session_id,
        "workspace_path": record.workspace_path,
    }, workspace=root)
    return record


def save_session_snapshot(
    session: SessionRecord,
    messages: list[dict],
    last_user_prompt: str | None = None,
    display_messages: list[dict] | None = None,
) -> bool:
    try:
        prompt = last_user_prompt if last_user_prompt is not None else _last_user_prompt(messages)
        updated_at = _utc_timestamp()
        visible_messages = (
            messages
            if display_messages is None
            else display_messages
        )
        payload = {
            "snapshot_version": SNAPSHOT_VERSION,
            "session_id": session.session_id,
            "created_at": session.created_at,
            "updated_at": updated_at,
            "workspace_path": session.workspace_path,
            "message_count": len(messages),
            "display_message_count": len(visible_messages),
            "last_user_prompt_preview": (
                _preview_text(prompt) if prompt else None
            ),
            "messages": _snapshot_messages(messages),
            "display_messages": _snapshot_messages(visible_messages),
        }
        ok = _write_json(session.path, payload)
        if ok:
            log_event("session_updated", {
                "session_id": session.session_id,
                "message_count": len(messages),
                "display_message_count": len(visible_messages),
                "updated_at": updated_at,
            }, workspace=Path(session.workspace_path))
        return ok
    except Exception:
        return False


def load_session(session_id: str, workspace: str | Path | None = None) -> dict[str, Any] | None:
    try:
        path = _session_path(session_id, workspace)
        return _normalize_loaded_session(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def list_recent_sessions(workspace: str | Path | None = None,
                         limit: int = 10) -> list[dict[str, Any]]:
    try:
        paths = sorted(
            session_dir(workspace).glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return []

    sessions = []
    for path in paths[:max(int(limit), 0)]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        sessions.append({
            "session_id": data.get("session_id"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "workspace_path": data.get("workspace_path"),
            "message_count": data.get("message_count", 0),
            "display_message_count": data.get(
                "display_message_count",
                data.get("message_count", 0),
            ),
            "last_user_prompt_preview": data.get("last_user_prompt_preview"),
            "path": str(path),
        })
    return sessions


def load_latest_session(workspace: str | Path | None = None) -> dict[str, Any] | None:
    recent = list_recent_sessions(workspace, limit=1)
    if not recent:
        return None
    session_id = recent[0].get("session_id")
    if not session_id:
        return None
    return load_session(str(session_id), workspace)


def archive_session(
    session_id: str,
    workspace: str | Path | None = None,
) -> Path | None:
    """Move an idle session snapshot out of the active session listing."""
    root = _workspace(workspace)
    source = _session_path(session_id, root)
    target = archived_session_file_path(session_id, root)
    lock_path = source.with_name(f".{source.name}.lock")
    try:
        with exclusive_file_lock(lock_path):
            if not source.exists():
                return None
            if target.exists():
                raise FileExistsError(
                    f"Archived session already exists: {session_id}"
                )
            os.replace(source, target)
        log_event(
            "session_archived",
            {"session_id": session_id},
            workspace=root,
        )
        return target
    except FileNotFoundError:
        return None
