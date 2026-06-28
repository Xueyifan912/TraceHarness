"""Bounded audit event reads and timeline projection for the Web API."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..runtime.events import event_log_path

DEFAULT_EVENT_LIMIT = 200
MAX_EVENT_LIMIT = 1000
MAX_SCAN_LINES = 5000
MAX_TAIL_BYTES = 4 * 1024 * 1024


def _bounded_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_EVENT_LIMIT
    return max(0, min(int(limit), MAX_EVENT_LIMIT))


def _tail_jsonl(path: Path, max_lines: int = MAX_SCAN_LINES,
                max_bytes: int = MAX_TAIL_BYTES) -> list[tuple[str, dict[str, Any]]]:
    if max_lines <= 0 or max_bytes <= 0 or not path.exists():
        return []

    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            file_size = handle.tell()
            remaining = min(file_size, max_bytes)
            position = file_size
            chunks: list[bytes] = []
            newline_count = 0

            while remaining > 0 and newline_count <= max_lines:
                read_size = min(8192, remaining)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
                remaining -= read_size

        data = b"".join(reversed(chunks))
        start_offset = position
        if start_offset > 0:
            first_newline = data.find(b"\n")
            if first_newline < 0:
                return []
            data = data[first_newline + 1:]
            start_offset += first_newline + 1

        records: list[tuple[str, dict[str, Any]]] = []
        offset = start_offset
        for raw_line in data.splitlines(keepends=True):
            line = raw_line.strip()
            current_offset = offset
            offset += len(raw_line)
            if not line:
                continue
            try:
                parsed = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if isinstance(parsed, dict):
                records.append((f"offset_{current_offset}", parsed))
        return records[-max_lines:]
    except Exception:
        return []


def _normalise_event(event_id: str, record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return {
        "event_id": str(record.get("event_id") or event_id),
        "ts": record.get("ts"),
        "type": str(record.get("type") or ""),
        "session_id": record.get("session_id"),
        "run_id": record.get("run_id"),
        "source": record.get("source"),
        "payload": payload,
    }


class EventStore:
    def __init__(self, workspace: str | Path | None = None):
        self.workspace = (Path.cwd() if workspace is None else Path(workspace)).resolve()

    @property
    def path(self) -> Path:
        return event_log_path(self.workspace)

    def read_events(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        event_type: str | None = None,
        limit: int | None = DEFAULT_EVENT_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        bounded_limit = _bounded_limit(limit)
        scan_lines = min(
            MAX_SCAN_LINES,
            max(200, bounded_limit * 20 if bounded_limit else 200),
        )
        raw_records = _tail_jsonl(self.path, max_lines=scan_lines)

        events = []
        for event_id, record in raw_records:
            event = _normalise_event(event_id, record)
            if session_id is not None and event.get("session_id") != session_id:
                continue
            if run_id is not None and event.get("run_id") != run_id:
                continue
            if event_type is not None and event.get("type") != event_type:
                continue
            events.append(event)

        warnings: list[str] = []
        if cursor:
            cursor_index = next(
                (
                    index
                    for index, event in enumerate(events)
                    if event.get("event_id") == cursor
                ),
                None,
            )
            if cursor_index is None:
                warnings.append(
                    "The requested event cursor is no longer available; "
                    "recent events were replayed."
                )
            else:
                events = events[cursor_index + 1:]

        selected = events[-bounded_limit:] if bounded_limit else []
        return {
            "events": selected,
            "next_cursor": (
                selected[-1].get("event_id")
                if selected
                else cursor
            ),
            "warnings": warnings,
        }

    def session_events(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        limit: int | None = DEFAULT_EVENT_LIMIT,
    ) -> dict[str, Any]:
        response = self.read_events(
            session_id=session_id,
            run_id=run_id,
            limit=limit,
        )
        response.setdefault("warnings", [])
        return response

    def timeline(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        limit: int | None = DEFAULT_EVENT_LIMIT,
    ) -> dict[str, Any]:
        events = self.read_events(
            session_id=session_id,
            run_id=run_id,
            limit=limit,
        )["events"]
        return {
            "items": build_timeline(events),
            "warnings": [],
        }


def build_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    tool_items: dict[str, dict[str, Any]] = {}
    llm_items: dict[str, dict[str, Any]] = {}
    open_llm_items: list[dict[str, Any]] = []

    for event in events:
        event_type = event.get("type")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        event_id = str(event.get("event_id") or len(items))
        timestamp = event.get("ts")

        if event_type == "tool_call_started":
            key = str(payload.get("tool_use_id") or event_id)
            item = tool_items.get(key)
            if item is None:
                item = {
                    "id": f"tool_{key}",
                    "type": "tool_call",
                    "title": payload.get("tool") or "tool",
                    "status": "running",
                    "tool_use_id": payload.get("tool_use_id"),
                }
                tool_items[key] = item
                items.append(item)
            item["started_at"] = timestamp
            item["title"] = payload.get("tool") or item["title"]
            if "input" in payload:
                item["input_preview"] = payload.get("input")
            continue

        if event_type == "tool_call_ended":
            key = str(payload.get("tool_use_id") or event_id)
            item = tool_items.get(key)
            if item is None:
                item = {
                    "id": f"tool_{key}",
                    "type": "tool_call",
                    "title": payload.get("tool") or "tool",
                    "tool_use_id": payload.get("tool_use_id"),
                }
                tool_items[key] = item
                items.append(item)
            item["status"] = payload.get("status") or "completed"
            item["ended_at"] = timestamp
            item["title"] = payload.get("tool") or item["title"]
            if "input" in payload and "input_preview" not in item:
                item["input_preview"] = payload.get("input")
            if "output_preview" in payload:
                item["output_preview"] = payload.get("output_preview")
            if "output_length" in payload:
                item["output_length"] = payload.get("output_length")
            continue

        if event_type == "llm_call_started":
            call_key = str(payload.get("llm_call_id") or event_id)
            item = {
                "id": f"llm_{call_key}",
                "type": "llm_call",
                "title": "LLM call",
                "status": "running",
                "started_at": timestamp,
                "model": payload.get("model"),
                "provider": payload.get("provider"),
            }
            items.append(item)
            llm_items[call_key] = item
            open_llm_items.append(item)
            continue

        if event_type in ("llm_call_ended", "llm_call_failed"):
            status = "failed" if event_type == "llm_call_failed" else "completed"
            call_id = payload.get("llm_call_id")
            call_key = str(call_id or event_id)
            item = llm_items.get(call_key) if call_id else None
            if item is None:
                item = next(
                    (candidate for candidate in reversed(open_llm_items)
                     if candidate.get("status") == "running"),
                    None,
                )
            if item is None:
                item = {
                    "id": f"llm_{call_key}",
                    "type": "llm_call",
                    "title": "LLM call",
                    "started_at": timestamp,
                }
                items.append(item)
                llm_items[call_key] = item
            item["status"] = status
            item["ended_at"] = timestamp
            item["model"] = payload.get("model") or item.get("model")
            item["provider"] = payload.get("provider") or item.get("provider")
            for key in (
                "stop_reason",
                "elapsed_seconds",
                "error_type",
                "message",
                "usage",
            ):
                if key in payload:
                    item[key] = payload.get(key)
            continue

        if event_type == "permission_decision":
            items.append({
                "id": f"permission_{event_id}",
                "type": "permission",
                "title": f"Permission: {payload.get('tool') or 'tool'}",
                "status": payload.get("action"),
                "timestamp": timestamp,
                "tool_use_id": payload.get("tool_use_id"),
                "reason": payload.get("reason"),
                "rule": payload.get("rule"),
                "subject": payload.get("subject"),
            })
            continue

        if event_type == "final_stop":
            items.append({
                "id": f"final_{event_id}",
                "type": "final_stop",
                "title": "Final stop",
                "status": "completed",
                "timestamp": timestamp,
                "reason": payload.get("reason"),
            })

    return items
