"""Structured status helpers for the Web inspector panels."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..mcp.client import (
    MOCK_SERVERS,
    StdioMCPClient,
    connect_mcp as connect_mcp_server,
    mcp_clients_snapshot,
    mcp_tool_entries,
    normalize_mcp_name,
)
from ..mcp.config import load_mcp_configs
from ..memory.store import APPEND_MAX_LENGTH, append_memory, memory_path
from ..runtime.execution import execution_context
from ..runtime.events import event_context, scrub_sensitive_text
from ..runtime.fileio import safe_runtime_path
from ..runtime.session import load_session
from ..teams import (
    active_teammates_snapshot,
    pending_requests_snapshot,
    team_status,
)
from ..tools.registry import BUILTIN_TOOLS
from .agent_service import SessionNotFound, WebApiError
from .event_store import EventStore

TASKS_DIR_NAME = ".tasks"
WORKTREES_DIR_NAME = ".worktrees"
MEMORY_READ_LIMIT = 50 * 1024


class McpServerNotFound(WebApiError):
    status_code = 404
    code = "mcp_server_not_found"
    message = "MCP server was not found."


class McpConnectFailed(WebApiError):
    status_code = 400
    code = "mcp_connect_failed"
    message = "MCP server connection failed."


class InvalidMemoryContent(WebApiError):
    status_code = 400
    code = "invalid_memory_content"
    message = "Memory content must not be empty."


class MemoryAppendFailed(WebApiError):
    status_code = 500
    code = "memory_append_failed"
    message = "Memory could not be appended."


def _utc_from_mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(
            path.stat().st_mtime,
            timezone.utc,
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except Exception:
        return None


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


class StatusService:
    def __init__(self, workspace: str | Path | None = None,
                 event_store: EventStore | None = None):
        self.workspace = (Path.cwd() if workspace is None else Path(workspace)).resolve()
        self.event_store = event_store or EventStore(self.workspace)

    def team_status(self) -> dict[str, Any]:
        return {
            "active_teammates": self.active_teammates(),
            "pending_requests": self.pending_requests(),
            "tasks": self.tasks()["tasks"],
            "worktrees": self.worktrees()["worktrees"],
            "raw_text": self._raw_team_status(),
        }

    def tasks(self) -> dict[str, Any]:
        tasks_dir = safe_runtime_path(
            self.workspace,
            TASKS_DIR_NAME,
            create_directory=True,
        )
        tasks: list[dict[str, Any]] = []
        try:
            paths = sorted(tasks_dir.glob("task_*.json"))
        except Exception:
            paths = []
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                tasks.append({
                    "id": str(data.get("id") or path.stem),
                    "subject": str(data.get("subject") or ""),
                    "description": str(data.get("description") or ""),
                    "status": str(data.get("status") or "unknown"),
                    "owner": data.get("owner"),
                    "blockedBy": (
                        data.get("blockedBy")
                        if isinstance(data.get("blockedBy"), list)
                        else []
                    ),
                    "worktree": data.get("worktree"),
                })
        return {"tasks": tasks}

    def worktrees(self) -> dict[str, Any]:
        worktrees_dir = safe_runtime_path(
            self.workspace,
            WORKTREES_DIR_NAME,
            create_directory=True,
        )
        task_by_worktree = {
            task.get("worktree"): task.get("id")
            for task in self.tasks()["tasks"]
            if task.get("worktree")
        }
        worktrees: list[dict[str, Any]] = []
        try:
            paths = sorted(worktrees_dir.iterdir())
        except Exception:
            paths = []
        for path in paths:
            if not path.is_dir():
                continue
            name = path.name
            worktrees.append({
                "name": name,
                "path": str(path),
                "branch": self._worktree_branch(path, name),
                "task_id": task_by_worktree.get(name) or "",
            })
        return {"worktrees": worktrees}

    def mcp_status(self) -> dict[str, Any]:
        configs = load_mcp_configs(self.workspace)
        configured = [
            {
                "name": config.name,
                "transport": "stdio",
                "command": scrub_sensitive_text(config.command),
                "args": [
                    scrub_sensitive_text(argument)
                    for argument in config.args
                ],
                "env_keys": sorted(config.env.keys()),
                "configured": True,
            }
            for config in configs.values()
        ]
        clients = mcp_clients_snapshot(self.workspace)
        connected = [
            self._connected_mcp_server(name, client)
            for name, client in sorted(clients.items())
        ]
        return {
            "mock_servers": sorted(MOCK_SERVERS.keys()),
            "configured_servers": sorted(configured, key=lambda item: item["name"]),
            "connected_servers": connected,
            "errors": self._recent_mcp_errors(),
        }

    def connect_mcp(self, name: str) -> dict[str, Any]:
        server_name = str(name or "").strip()
        if not server_name:
            raise McpConnectFailed(
                "MCP server name must not be empty.",
                details={"name": name},
            )

        message = connect_mcp_server(server_name, workspace=self.workspace)
        client = mcp_clients_snapshot(self.workspace).get(server_name)
        if client is None:
            details = {"name": server_name, "message": message}
            if message.startswith("Unknown server"):
                raise McpServerNotFound(message, details=details)
            raise McpConnectFailed(message, details=details)

        return {
            "ok": True,
            "message": message,
            "server": self._connected_mcp_server(server_name, client),
        }

    def tools(self) -> dict[str, Any]:
        tools = [
            {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or ""),
                "source": "builtin",
                "server": None,
                "input_schema": tool.get("input_schema") or {},
            }
            for tool in BUILTIN_TOOLS
        ]

        with event_context(source="web", workspace=self.workspace):
            mcp_tools, _handlers = mcp_tool_entries(self.workspace)
        server_by_name = self._mcp_server_by_prefixed_tool()
        for tool in mcp_tools:
            name = str(tool.get("name") or "")
            tools.append({
                "name": name,
                "description": str(tool.get("description") or ""),
                "source": "mcp",
                "server": server_by_name.get(name),
                "input_schema": tool.get("input_schema") or {},
            })
        return {"tools": tools}

    def memory(self) -> dict[str, Any]:
        path = memory_path(self.workspace)
        exists = path.exists()
        content = ""
        truncated = False
        size = 0
        if exists:
            try:
                size = path.stat().st_size
                with path.open("r", encoding="utf-8") as handle:
                    content = handle.read(MEMORY_READ_LIMIT + 1)
                if len(content) > MEMORY_READ_LIMIT:
                    content = content[:MEMORY_READ_LIMIT]
                    truncated = True
            except Exception:
                content = ""
                truncated = False
        return {
            "path": str(path),
            "exists": exists,
            "length": len(content),
            "size_bytes": size,
            "updated_at": _utc_from_mtime(path) if exists else None,
            "content": content,
            "truncated": truncated,
            "limit": MEMORY_READ_LIMIT,
        }

    def append_memory(self, content: str,
                      session_id: str | None = None) -> dict[str, Any]:
        resolved_session_id = self._validate_session_id(session_id)
        text = str(content or "").strip()
        if not text:
            raise InvalidMemoryContent()
        with event_context(
            session_id=resolved_session_id,
            source="web",
            workspace=self.workspace,
        ):
            message = append_memory(text, self.workspace)
        if message.startswith("Error:"):
            raise MemoryAppendFailed(
                details={"error_type": "memory_store_error"},
            )
        current = self.memory()
        return {
            "ok": True,
            "message": message,
            "length": len(text),
            "max_length": APPEND_MAX_LENGTH,
            "memory": current,
        }

    def _validate_session_id(self, session_id: str | None) -> str | None:
        if session_id is None:
            return None
        candidate = str(session_id)
        if not load_session(candidate, self.workspace):
            raise SessionNotFound(details={"session_id": candidate})
        return candidate

    def active_teammates(self) -> list[dict[str, Any]]:
        teammates = []
        for name, info in sorted(active_teammates_snapshot().items()):
            item = _jsonable(info)
            if not isinstance(item, dict):
                item = {}
            item.setdefault("name", name)
            teammates.append(item)
        return teammates

    def pending_requests(self) -> list[dict[str, Any]]:
        requests = []
        for request_id, request in sorted(pending_requests_snapshot().items()):
            item = _jsonable(request)
            if not isinstance(item, dict):
                item = {"request_id": request_id}
            item.setdefault("request_id", request_id)
            requests.append(item)
        return requests

    def _connected_mcp_server(self, name: str, client: Any) -> dict[str, Any]:
        safe_server = normalize_mcp_name(name)
        tools = []
        for tool in getattr(client, "tools", []) or []:
            tool_name = str(tool.get("name") or "")
            safe_tool = normalize_mcp_name(tool_name)
            tools.append({
                "name": f"mcp__{safe_server}__{safe_tool}",
                "raw_name": tool_name,
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema", {}),
            })
        if isinstance(client, StdioMCPClient):
            transport = "stdio"
        elif name in MOCK_SERVERS:
            transport = "mock"
        else:
            transport = "unknown"
        return {
            "name": name,
            "transport": transport,
            "tool_count": len(tools),
            "tools": tools,
        }

    def _mcp_server_by_prefixed_tool(self) -> dict[str, str]:
        server_by_name: dict[str, str] = {}
        for server_name, client in mcp_clients_snapshot(self.workspace).items():
            safe_server = normalize_mcp_name(server_name)
            for tool in getattr(client, "tools", []) or []:
                tool_name = str(tool.get("name") or "")
                safe_tool = normalize_mcp_name(tool_name)
                prefixed = f"mcp__{safe_server}__{safe_tool}"
                server_by_name.setdefault(prefixed, server_name)
        return server_by_name

    def _recent_mcp_errors(self) -> list[dict[str, Any]]:
        response = self.event_store.read_events(limit=200)
        errors = []
        for event in response["events"]:
            event_type = event.get("type")
            payload = event.get("payload") or {}
            if event_type == "mcp_connect" and payload.get("ok") is False:
                errors.append({
                    "type": event_type,
                    "ts": event.get("ts"),
                    "server": payload.get("server"),
                    "message": payload.get("error"),
                })
            elif event_type == "mcp_tool_name_collision":
                errors.append({
                    "type": event_type,
                    "ts": event.get("ts"),
                    "prefixed_name": payload.get("prefixed_name"),
                    "server": payload.get("server"),
                    "tool": payload.get("tool"),
                })
        return errors[-20:]

    def _raw_team_status(self) -> str:
        try:
            with execution_context(workspace=self.workspace, source="web"):
                return team_status()
        except Exception as exc:
            return f"team_status unavailable: {type(exc).__name__}: {exc}"

    @staticmethod
    def _worktree_branch(path: Path, name: str) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            branch = result.stdout.strip()
            if result.returncode == 0 and branch:
                return branch
        except Exception:
            pass
        return f"wt/{name}"
