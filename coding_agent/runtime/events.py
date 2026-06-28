"""Small JSONL audit event sink for the local workspace."""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from .fileio import append_text_locked, safe_runtime_path

EVENT_DIR = ".agent_events"
EVENT_FILE = "events.jsonl"
_WRITE_LOCK = threading.Lock()
_LISTENER_LOCK = threading.Lock()
_EVENT_LISTENERS: list[Callable[[dict[str, Any]], None]] = []
_PREVIEW_LIMIT = 2000
_INPUT_PREVIEW_LIMIT = 500
_MAX_PREVIEW_ITEMS = 20
_MAX_PREVIEW_KEYS = 50
_SENSITIVE_KEY_PARTS = (
    "key",
    "token",
    "secret",
    "password",
    "authorization",
)
_NON_SENSITIVE_KEYS = {
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "max_tokens",
}
_OMITTED_TOOL_INPUT_FIELDS = {
    "memory_append": {"content"},
}
_EVENT_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "event_context",
    default=None,
)
_EVENT_CONTEXT_RECORD_KEYS = ("session_id", "run_id", "source")


def event_log_path(workspace: str | Path | None = None) -> Path:
    """Return the workspace-local event log path."""
    base = Path.cwd() if workspace is None else Path(workspace)
    return safe_runtime_path(base, EVENT_DIR, EVENT_FILE)


def current_event_context() -> dict[str, Any]:
    context = _EVENT_CONTEXT.get()
    return dict(context) if context else {}


def register_event_listener(
    listener: Callable[[dict[str, Any]], None],
) -> Callable[[], None]:
    """Register a best-effort in-process listener for newly written events."""
    with _LISTENER_LOCK:
        _EVENT_LISTENERS.append(listener)

    def unsubscribe() -> None:
        with _LISTENER_LOCK:
            try:
                _EVENT_LISTENERS.remove(listener)
            except ValueError:
                pass

    return unsubscribe


def _notify_event_listeners(record: dict[str, Any]) -> None:
    with _LISTENER_LOCK:
        listeners = list(_EVENT_LISTENERS)
    for listener in listeners:
        try:
            listener(dict(record))
        except Exception:
            continue


@contextmanager
def event_context(
    *,
    session_id: str | None = None,
    run_id: str | None = None,
    source: str | None = None,
    workspace: str | Path | None = None,
    clear_run_id: bool = False,
) -> Iterator[None]:
    next_context = current_event_context()
    if session_id is not None:
        next_context["session_id"] = str(session_id)
    if clear_run_id:
        next_context.pop("run_id", None)
    elif run_id is not None:
        next_context["run_id"] = str(run_id)
    if source is not None:
        next_context["source"] = str(source)
    if workspace is not None:
        next_context["workspace"] = str(Path(workspace).resolve())

    token = _EVENT_CONTEXT.set(next_context)
    try:
        yield
    finally:
        _EVENT_CONTEXT.reset(token)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _preview(value: Any, limit: int = _PREVIEW_LIMIT) -> str:
    text = scrub_sensitive_text(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... ({len(text) - limit} more chars)"


def _value_length(value: Any) -> int:
    try:
        if isinstance(value, str):
            return len(value)
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        try:
            return len(str(value))
        except Exception:
            return 0


def _is_sensitive_key(key: Any) -> bool:
    lowered = str(key).lower()
    if lowered in _NON_SENSITIVE_KEYS:
        return False
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _redacted_value(value: Any) -> dict[str, Any]:
    return {"redacted": True, "length": _value_length(value)}


def _text_preview(value: Any, limit: int = _INPUT_PREVIEW_LIMIT) -> dict[str, Any]:
    original_text = str(value)
    text = scrub_sensitive_text(original_text)
    return {
        "preview": _preview(text, limit),
        "length": len(original_text),
        "truncated": len(original_text) > limit,
    }


_TEXT_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)"
        r"\s*[:=]\s*([^\s,;]+)"
    ),
)


def scrub_sensitive_text(value: Any) -> str:
    text = str(value)
    text = _TEXT_SECRET_PATTERNS[0].sub("Bearer [REDACTED]", text)
    text = _TEXT_SECRET_PATTERNS[1].sub("[REDACTED_KEY]", text)
    text = _TEXT_SECRET_PATTERNS[2].sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        text,
    )
    return text


def safe_text_preview(
    value: Any,
    limit: int = _INPUT_PREVIEW_LIMIT,
) -> dict[str, Any]:
    return _text_preview(value, limit)


def _payload_preview(value: Any, limit: int = _INPUT_PREVIEW_LIMIT) -> Any:
    """Return a bounded, redacted representation for free-form payload values."""
    try:
        if isinstance(value, str):
            return _text_preview(value, limit)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, dict):
            items = list(value.items())
            preview = {}
            for key, item in items[:_MAX_PREVIEW_KEYS]:
                preview[str(key)] = (
                    _redacted_value(item)
                    if _is_sensitive_key(key)
                    else _payload_preview(item, limit)
                )
            payload = {
                "type": "object",
                "length": _value_length(value),
                "value": preview,
            }
            if len(items) > _MAX_PREVIEW_KEYS:
                payload["omitted_keys"] = len(items) - _MAX_PREVIEW_KEYS
            return payload
        if isinstance(value, (list, tuple)):
            items = list(value)
            payload = {
                "type": "array",
                "length": _value_length(value),
                "item_count": len(items),
                "value": [
                    _payload_preview(item, limit)
                    for item in items[:_MAX_PREVIEW_ITEMS]
                ],
            }
            if len(items) > _MAX_PREVIEW_ITEMS:
                payload["omitted_items"] = len(items) - _MAX_PREVIEW_ITEMS
            return payload
        return _text_preview(value, limit)
    except Exception:
        return {"preview": "<unavailable>", "length": _value_length(value),
                "truncated": False}


def _omitted_input_value(value: Any) -> dict[str, Any]:
    return {
        "omitted": True,
        "length": _value_length(value),
        "truncated": _value_length(value) > _INPUT_PREVIEW_LIMIT,
    }


def _tool_input_payload(tool_name: str, value: Any) -> Any:
    omitted_fields = _OMITTED_TOOL_INPUT_FIELDS.get(tool_name, set())
    if not omitted_fields or not isinstance(value, dict):
        return _payload_preview(value)

    items = list(value.items())
    preview = {}
    for key, item in items[:_MAX_PREVIEW_KEYS]:
        key_text = str(key)
        if key_text in omitted_fields:
            preview[key_text] = _omitted_input_value(item)
        elif _is_sensitive_key(key_text):
            preview[key_text] = _redacted_value(item)
        else:
            preview[key_text] = _payload_preview(item)

    payload = {
        "type": "object",
        "length": _value_length(value),
        "value": preview,
    }
    if len(items) > _MAX_PREVIEW_KEYS:
        payload["omitted_keys"] = len(items) - _MAX_PREVIEW_KEYS
    return payload


def _redact_sensitive_fields(value: Any) -> Any:
    try:
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                key_text = str(key)
                if _is_sensitive_key(key_text):
                    redacted[key_text] = (
                        item if isinstance(item, dict) and item.get("redacted")
                        else _redacted_value(item)
                    )
                else:
                    redacted[key_text] = _redact_sensitive_fields(item)
            return redacted
        if isinstance(value, list):
            return [_redact_sensitive_fields(item) for item in value]
        if isinstance(value, tuple):
            return [_redact_sensitive_fields(item) for item in value]
        return value
    except Exception:
        return value


def _usage_payload(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    payload = {}
    for name in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                 "cache_read_input_tokens"):
        value = getattr(usage, name, None)
        if value is not None:
            payload[name] = value
    return payload or None


def _block_payload(block: Any) -> dict[str, Any]:
    tool_name = getattr(block, "name", None)
    return {
        "tool": tool_name,
        "tool_use_id": getattr(block, "id", None),
        "input": _tool_input_payload(str(tool_name), getattr(block, "input", None)),
    }


def log_event(event_type: str, payload: dict[str, Any] | None = None,
              workspace: str | Path | None = None) -> bool:
    """Append one event record.

    Logging is deliberately best-effort: any filesystem or serialization
    failure is swallowed so auditability never changes agent control flow.
    """
    try:
        context = current_event_context()
        record = {
            "event_id": f"evt_{uuid4().hex}",
            "ts": _utc_timestamp(),
            "type": str(event_type),
            "payload": _redact_sensitive_fields(payload or {}),
        }
        for key in _EVENT_CONTEXT_RECORD_KEYS:
            value = context.get(key)
            if value is not None:
                record[key] = str(value)
        line = json.dumps(record, ensure_ascii=False, default=str)
        path = event_log_path(workspace or context.get("workspace"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK:
            append_text_locked(path, line + "\n")
        _notify_event_listeners(record)
        return True
    except Exception:
        return False


def log_user_prompt_submission(prompt: str) -> bool:
    return log_event("user_prompt_submitted", {"prompt": _payload_preview(prompt)})


def log_llm_call_started(model: str, message_count: int, tool_count: int,
                         max_tokens: int, timeout_seconds: float,
                         provider: str | None = None,
                         call_id: str | None = None) -> bool:
    payload = {
        "llm_call_id": call_id,
        "model": model,
        "message_count": message_count,
        "tool_count": tool_count,
        "max_tokens": max_tokens,
        "timeout_seconds": timeout_seconds,
    }
    if provider:
        payload["provider"] = provider
    return log_event("llm_call_started", payload)


def log_llm_call_ended(response: Any, elapsed_seconds: float,
                       model: str | None = None,
                       provider: str | None = None,
                       call_id: str | None = None) -> bool:
    content = getattr(response, "content", None)
    try:
        content_block_count = len(content) if content is not None else None
    except TypeError:
        content_block_count = None
    payload = {
        "llm_call_id": call_id,
        "model": model or getattr(response, "model", None),
        "response_id": getattr(response, "id", None),
        "stop_reason": getattr(response, "stop_reason", None),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "content_block_count": content_block_count,
    }
    if provider:
        payload["provider"] = provider
    usage = _usage_payload(getattr(response, "usage", None))
    if usage:
        payload["usage"] = usage
    return log_event("llm_call_ended", payload)


def log_llm_call_failed(error: BaseException, elapsed_seconds: float,
                        model: str | None = None,
                        provider: str | None = None,
                        call_id: str | None = None) -> bool:
    payload = {
        "llm_call_id": call_id,
        "model": model,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "error_type": type(error).__name__,
        "message": _text_preview(error),
    }
    if provider:
        payload["provider"] = provider
    return log_event("llm_call_failed", payload)


def log_tool_call_started(block: Any) -> bool:
    return log_event("tool_call_started", _block_payload(block))


def log_tool_call_ended(block: Any, output: Any = None,
                        status: str = "completed") -> bool:
    payload = _block_payload(block)
    payload["status"] = status
    if output is not None:
        output_text = str(output)
        payload["output_length"] = len(output_text)
        payload["output_preview"] = _preview(output_text)
    return log_event("tool_call_ended", payload)


def log_permission_denied(block: Any, reason: str) -> bool:
    payload = _block_payload(block)
    payload["reason"] = reason
    return log_event("permission_denied", payload)


def log_final_stop(reason: str, message_count: int | None = None,
                   tool_result_count: int | None = None,
                   **extra: Any) -> bool:
    payload = {"reason": reason}
    if message_count is not None:
        payload["message_count"] = message_count
    if tool_result_count is not None:
        payload["tool_result_count"] = tool_result_count
    payload.update(extra)
    return log_event("final_stop", payload)
