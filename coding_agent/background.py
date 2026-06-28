import threading
from contextlib import nullcontext

from .hooks import trigger_hooks
from .runtime.cancellation import (
    cancellation_context,
    current_cancellation_event,
)
from .runtime.execution import (
    current_execution_context,
    execution_context,
)
from .runtime.events import current_event_context, event_context, log_event
from .tools.basic import call_tool_handler

# ── Background Tasks ──

# Slow tools return a placeholder tool_result immediately. Their real output is
# later injected as a task_notification, so the main loop can keep moving.
_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()
background_condition = threading.Condition(background_lock)
_CONTEXT_KEYS = ("session_id", "run_id", "source", "workspace")
_RESULT_SCOPE_KEYS = ("session_id", "run_id", "workspace")


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    if tool_name != "bash":
        return False
    command = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(keyword in command for keyword in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    if tool_name != "bash":
        return False
    return bool(tool_input.get("run_in_background")) or is_slow_operation(tool_name, tool_input)


def _captured_context() -> dict:
    execution = current_execution_context()
    context = current_event_context()
    context.update(execution)
    return {
        key: str(context[key])
        for key in _CONTEXT_KEYS
        if context.get(key) is not None
    }


def _matches_current_context(task: dict, current: dict) -> bool:
    task_context = task.get("context") or {}
    for key in _RESULT_SCOPE_KEYS:
        task_value = task_context.get(key)
        current_value = current.get(key)
        if task_value is not None or current_value is not None:
            if task_value != current_value:
                return False

    return True


def start_background_task(block, handlers: dict) -> str:
    global _bg_counter
    with background_lock:
        _bg_counter += 1
        bg_id = f"bg_{_bg_counter:04d}"
    command = block.input.get("command", block.name)
    captured_context = _captured_context()
    captured_execution = current_execution_context()
    captured_event = current_event_context()
    captured_cancel_event = current_cancellation_event()

    def worker():
        status = "completed"
        error_type = None
        cancel_scope = (
            cancellation_context(captured_cancel_event)
            if captured_cancel_event is not None
            else nullcontext()
        )
        with cancel_scope, execution_context(
                session_id=captured_execution.get("session_id"),
                run_id=captured_execution.get("run_id"),
                source=captured_execution.get("source"),
                workspace=captured_execution.get("workspace"),
                detached=captured_execution.get("detached"),
            ), event_context(
                session_id=captured_event.get("session_id"),
                run_id=captured_event.get("run_id"),
                source=captured_event.get("source"),
                workspace=captured_event.get("workspace"),
            ):
            try:
                handler = handlers.get(block.name)
                result = call_tool_handler(handler, block.input, block.name)
                trigger_hooks("PostToolUse", block, result)
            except Exception as exc:
                status = "failed"
                error_type = type(exc).__name__
                result = f"Error: {error_type}: {exc}"

            with background_condition:
                task = background_tasks.get(bg_id)
                if task is None:
                    return
                if task.get("abandoned") or (
                    captured_cancel_event is not None
                    and captured_cancel_event.is_set()
                ):
                    background_tasks.pop(bg_id, None)
                    background_results.pop(bg_id, None)
                    background_condition.notify_all()
                    return
                payload = {
                    "background_id": bg_id,
                    "tool_use_id": block.id,
                    "tool": block.name,
                    "status": status,
                }
                if error_type:
                    payload["error_type"] = error_type
                log_event("background_completion", {
                    **payload,
                })
                task["status"] = status
                background_results[bg_id] = str(result)
                background_condition.notify_all()

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": command,
            "status": "running",
            "context": captured_context,
            "session_id": captured_context.get("session_id"),
            "run_id": captured_context.get("run_id"),
            "workspace": captured_context.get("workspace"),
            "abandoned": False,
        }
    thread = threading.Thread(target=worker, daemon=True)
    with background_condition:
        task = background_tasks.get(bg_id)
        if task is not None:
            task["thread"] = thread
    try:
        thread.start()
    except Exception:
        with background_condition:
            background_tasks.pop(bg_id, None)
            background_results.pop(bg_id, None)
            background_condition.notify_all()
        raise
    print(f"  \033[33m[background] {bg_id}: {str(command)[:60]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    current_context = _captured_context()
    with background_lock:
        ready = [bg_id for bg_id, task in background_tasks.items()
                 if task["status"] in {"completed", "failed"}
                 and _matches_current_context(task, current_context)]
    notifications = []
    for bg_id in ready:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        summary = output[:200] if len(output) > 200 else output
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>{task['status']}</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
    return notifications


def has_outstanding_background_tasks() -> bool:
    """Return whether this exact execution scope still owns background work."""
    current_context = _captured_context()
    with background_lock:
        return any(
            task["status"] in {"running", "completed", "failed"}
            and _matches_current_context(task, current_context)
            for task in background_tasks.values()
        )


def wait_for_background_task_update(timeout: float = 0.1) -> bool:
    """Wait briefly and report whether scoped background work is still running."""
    current_context = _captured_context()

    def has_running() -> bool:
        return any(
            task["status"] == "running"
            and _matches_current_context(task, current_context)
            for task in background_tasks.values()
        )

    with background_condition:
        if has_running():
            background_condition.wait(timeout=max(float(timeout), 0.0))
        return has_running()


def abandon_background_tasks(*, wait: bool = False) -> None:
    """Cancel delivery and optionally wait for scoped background work to exit."""
    current_context = _captured_context()
    with background_condition:
        for bg_id, task in list(background_tasks.items()):
            if not _matches_current_context(task, current_context):
                continue
            if task["status"] == "running":
                task["abandoned"] = True
            else:
                background_tasks.pop(bg_id, None)
                background_results.pop(bg_id, None)
        background_condition.notify_all()
        while wait and any(
            _matches_current_context(task, current_context)
            for task in background_tasks.values()
        ):
            background_condition.wait(timeout=0.1)

