"""Configurable safety policy for tool permission decisions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
import re

import yaml

from ..config import WORKDIR
from ..runtime.events import log_event, safe_text_preview
from ..runtime.fileio import safe_runtime_path

POLICY_FILE = ".agent_policy.yaml"
_SUBJECT_PREVIEW_LIMIT = 500


@dataclass(frozen=True)
class SecurityPolicy:
    bash_deny_patterns: tuple[str, ...]
    bash_ask_patterns: tuple[str, ...]
    bash_default_action: str
    guarded_file_tools: tuple[str, ...]
    mcp_ask_name_patterns: tuple[str, ...]


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    tool: str
    reason: str
    rule: str = ""
    subject: str = ""
    source: str = "policy"
    tool_use_id: str | None = None

    def with_outcome(self, action: str, reason: str,
                     source: str = "policy") -> "PolicyDecision":
        return replace(self, action=action, reason=reason, source=source)

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "action": self.action,
            "tool": self.tool,
            "reason": self.reason,
            "source": self.source,
        }
        if self.rule:
            payload["rule"] = self.rule
        if self.subject:
            payload["subject"] = _subject_metadata(self.subject)
        if self.tool_use_id:
            payload["tool_use_id"] = self.tool_use_id
        return payload


def _subject_metadata(subject: Any) -> dict[str, Any]:
    return safe_text_preview(subject, limit=_SUBJECT_PREVIEW_LIMIT)


def default_policy() -> SecurityPolicy:
    return SecurityPolicy(
        bash_deny_patterns=(
            "rm -rf /",
            "sudo",
            "shutdown",
            "reboot",
            "mkfs",
            "dd if=",
            "format.com",
            "stop-computer",
            "restart-computer",
        ),
        bash_ask_patterns=(
            "rm ",
            "> /etc/",
            "chmod 777",
            "del ",
            "erase ",
            "rmdir ",
            "rd ",
            "remove-item",
            "clear-content",
            "set-content",
            "move-item",
            "rename-item",
            "powershell ",
            "powershell.exe ",
            "pwsh ",
            "pwsh.exe ",
            "../",
            "..\\",
            ":\\",
            ":/",
            "\\\\",
            "%userprofile%",
            "$home",
            "~/",
            "~\\",
        ),
        bash_default_action="ask",
        guarded_file_tools=("write_file", "edit_file"),
        mcp_ask_name_patterns=("deploy",),
    )


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return (str(value),)


def _configured_tuple(section: dict[str, Any], current: tuple[str, ...],
                      override_key: str, extend_key: str) -> tuple[str, ...]:
    values = (
        _as_tuple(section[override_key])
        if override_key in section
        else tuple(current)
    )
    return values + _as_tuple(section.get(extend_key))


def _load_policy_config(workspace: Path) -> dict[str, Any]:
    path = safe_runtime_path(workspace, POLICY_FILE)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_policy(workspace: str | Path | None = None) -> SecurityPolicy:
    base = default_policy()
    root = Path(workspace) if workspace is not None else WORKDIR
    data = _load_policy_config(root)

    bash = data.get("bash") if isinstance(data.get("bash"), dict) else {}
    file_tools = (
        data.get("file_tools")
        if isinstance(data.get("file_tools"), dict)
        else {}
    )
    mcp = data.get("mcp") if isinstance(data.get("mcp"), dict) else {}
    bash_default_action = str(
        bash.get("default_action", base.bash_default_action)
    ).strip().lower()
    if bash_default_action not in {"allow", "ask", "deny"}:
        bash_default_action = base.bash_default_action

    return SecurityPolicy(
        bash_deny_patterns=_configured_tuple(
            bash, base.bash_deny_patterns,
            "deny_patterns", "extend_deny_patterns"),
        bash_ask_patterns=_configured_tuple(
            bash, base.bash_ask_patterns,
            "ask_patterns", "extend_ask_patterns"),
        bash_default_action=bash_default_action,
        guarded_file_tools=_configured_tuple(
            file_tools, base.guarded_file_tools,
            "guarded_tools", "extend_guarded_tools"),
        mcp_ask_name_patterns=_configured_tuple(
            mcp, base.mcp_ask_name_patterns,
            "ask_name_patterns", "extend_ask_name_patterns"),
    )


def _block_tool(block: Any) -> str:
    return str(getattr(block, "name", ""))


def _block_input(block: Any) -> dict[str, Any]:
    tool_input = getattr(block, "input", None)
    return tool_input if isinstance(tool_input, dict) else {}


def _tool_use_id(block: Any) -> str | None:
    value = getattr(block, "id", None)
    return str(value) if value is not None else None


def _normalise_command_for_policy(command: str) -> str:
    return re.sub(r"\s+", " ", command).strip().casefold()


def _workspace_path_escapes(path: str, workspace: str | Path) -> bool:
    base = Path(workspace).resolve()
    try:
        resolved = (base / path).resolve()
        return not resolved.is_relative_to(base)
    except Exception:
        return True


def evaluate_tool_use(block: Any, policy: SecurityPolicy | None = None,
                      workspace: str | Path | None = None) -> PolicyDecision:
    active_policy = policy or load_policy(workspace)
    root = Path(workspace) if workspace is not None else WORKDIR
    tool = _block_tool(block)
    tool_input = _block_input(block)
    tool_use_id = _tool_use_id(block)

    if tool == "bash":
        command = str(tool_input.get("command", ""))
        normalised_command = _normalise_command_for_policy(command)
        for pattern in active_policy.bash_deny_patterns:
            normalised_pattern = _normalise_command_for_policy(pattern)
            if normalised_pattern and normalised_pattern in normalised_command:
                return PolicyDecision(
                    action="deny",
                    tool=tool,
                    reason=f"Permission denied: '{pattern}' is on the deny list",
                    rule=pattern,
                    subject=command,
                    tool_use_id=tool_use_id,
                )
        for pattern in active_policy.bash_ask_patterns:
            normalised_pattern = _normalise_command_for_policy(pattern)
            if normalised_pattern and normalised_pattern in normalised_command:
                return PolicyDecision(
                    action="ask",
                    tool=tool,
                    reason="Destructive-looking bash command",
                    rule=pattern,
                    subject=command,
                    tool_use_id=tool_use_id,
                )
        if active_policy.bash_default_action != "allow":
            action = active_policy.bash_default_action
            reason = (
                "Shell commands require explicit approval"
                if action == "ask"
                else "Permission denied: shell commands are disabled"
            )
            return PolicyDecision(
                action=action,
                tool=tool,
                reason=reason,
                rule="bash_default_action",
                subject=command,
                tool_use_id=tool_use_id,
            )

    if tool in active_policy.guarded_file_tools:
        path = str(tool_input.get("path", ""))
        if _workspace_path_escapes(path, root):
            return PolicyDecision(
                action="deny",
                tool=tool,
                reason=f"Permission denied: path escapes workspace: {path}",
                rule="workspace_path",
                subject=path,
                tool_use_id=tool_use_id,
            )

    if tool == "remove_worktree":
        name = str(tool_input.get("name", ""))
        discard = bool(tool_input.get("discard_changes"))
        return PolicyDecision(
            action="ask",
            tool=tool,
            reason=(
                "Removing a worktree can permanently delete uncommitted changes"
                if discard
                else "Removing a worktree and its branch requires confirmation"
            ),
            rule="worktree_removal",
            subject=f"name={name}, discard_changes={discard}",
            tool_use_id=tool_use_id,
        )

    if tool == "connect_mcp":
        name = str(tool_input.get("name", ""))
        return PolicyDecision(
            action="ask",
            tool=tool,
            reason="Connecting an MCP server may start a workspace-configured process",
            rule="mcp_process_start",
            subject=name,
            tool_use_id=tool_use_id,
        )

    if tool.startswith("mcp__"):
        for pattern in active_policy.mcp_ask_name_patterns:
            if pattern and pattern in tool:
                return PolicyDecision(
                    action="ask",
                    tool=tool,
                    reason=f"MCP destructive-looking tool: {tool}",
                    rule=pattern,
                    subject=tool,
                    tool_use_id=tool_use_id,
                )

    return PolicyDecision(
        action="allow",
        tool=tool,
        reason="Allowed by policy",
        tool_use_id=tool_use_id,
    )


def audit_policy_decision(decision: PolicyDecision) -> bool:
    return log_event("permission_decision", decision.to_payload())
