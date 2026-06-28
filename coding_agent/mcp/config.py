"""Workspace-local MCP server configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..runtime.execution import current_execution_context, execution_workspace
from ..runtime.fileio import safe_runtime_path

MCP_JSON = ".mcp.json"
MCP_YAML = ".agent_mcp.yaml"


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    workspace: str | None = None


def _workspace(workspace: str | Path | None = None) -> Path:
    if workspace is not None:
        return Path(workspace).resolve()
    if current_execution_context().get("workspace"):
        return execution_workspace()
    return Path.cwd().resolve()


def _load_config_data(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _server_map(data: dict[str, Any]) -> dict[str, Any]:
    servers = data.get("servers")
    if isinstance(servers, dict):
        return servers
    if isinstance(data, dict):
        return data
    return {}


def load_mcp_configs(
        workspace: str | Path | None = None) -> dict[str, MCPServerConfig]:
    root = _workspace(workspace)
    data = {}
    for filename in (MCP_JSON, MCP_YAML):
        data = _load_config_data(safe_runtime_path(root, filename))
        if data:
            break

    configs: dict[str, MCPServerConfig] = {}
    for name, raw in _server_map(data).items():
        if not isinstance(raw, dict):
            continue
        command = raw.get("command")
        if not command:
            continue
        args = raw.get("args", [])
        env = raw.get("env", {})
        configs[str(name)] = MCPServerConfig(
            name=str(name),
            command=str(command),
            args=[str(arg) for arg in args] if isinstance(args, list) else [],
            env={str(key): str(value) for key, value in env.items()}
            if isinstance(env, dict) else {},
            workspace=str(root),
        )
    return configs
