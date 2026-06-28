from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable

from .config import WORKDIR
from .runtime.events import event_context
from .runtime.execution import execution_workspace
from .security.policy import audit_policy_decision, evaluate_tool_use

# ── Hooks + Permission Pipeline ──

# Hooks are intentionally outside tool handlers. The loop can add permission,
# logging, and stop behavior without changing each individual tool.
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [],
         "PostToolUse": [], "Stop": []}

PermissionResolver = Callable[[object], str | None]
_PERMISSION_RESOLVER: ContextVar[PermissionResolver | None] = ContextVar(
    "permission_resolver",
    default=None,
)


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


def cli_permission_resolver(decision):
    if decision.action == "deny":
        return decision.reason
    if decision.action == "ask":
        if decision.tool == "bash":
            print(f"\n\033[33m[permission] destructive command\033[0m")
            print(f"  {decision.subject}")
        elif decision.tool.startswith("mcp__"):
            print(f"\n\033[33m[permission] {decision.reason}\033[0m")
        else:
            print(f"\n\033[33m[permission] {decision.reason}\033[0m")
        choice = input("  Allow? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
            audit_policy_decision(
                decision.with_outcome(
                    "deny", "Permission denied by user", "user_confirmation"))
            return "Permission denied by user"
        audit_policy_decision(
                decision.with_outcome(
                    "allow", "Permission allowed by user", "user_confirmation"))
    return None


def web_child_permission_resolver(decision):
    if decision.action == "deny":
        return decision.reason
    if decision.action == "ask":
        reason = (
            "Permission denied: detached Web teammate cannot await approval."
        )
        with event_context(source="web_child_auto_deny"):
            audit_policy_decision(
                decision.with_outcome(
                    "deny",
                    reason,
                    "web_child_auto_deny",
                )
            )
        return reason
    return None


@contextmanager
def use_permission_resolver(resolver: PermissionResolver):
    token = _PERMISSION_RESOLVER.set(resolver)
    try:
        yield
    finally:
        _PERMISSION_RESOLVER.reset(token)


def permission_hook(block):
    # The permission layer sees the raw tool_use before dispatch. It can deny,
    # ask the user, or allow execution to continue.
    decision = evaluate_tool_use(block, workspace=execution_workspace(WORKDIR))
    audit_policy_decision(decision)
    resolver = _PERMISSION_RESOLVER.get() or cli_permission_resolver
    return resolver(decision)


def log_hook(block):
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None


def large_output_hook(block, output):
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] large output from {block.name}: "
              f"{len(str(output))} chars\033[0m")
    return None


def user_prompt_hook(query: str):
    print(f"\033[90m[HOOK] UserPromptSubmit: {execution_workspace(WORKDIR)}\033[0m")
    return None


def stop_hook(messages: list):
    tool_count = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            tool_count += sum(1 for item in content
                              if isinstance(item, dict)
                              and item.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: {tool_count} tool result(s)\033[0m")
    return None


register_hook("UserPromptSubmit", user_prompt_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", stop_hook)

