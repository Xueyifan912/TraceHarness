from ..config import MODEL, REQUEST_TIMEOUT_SECONDS, WORKDIR
from ..hooks import trigger_hooks
from ..message_utils import extract_text, has_tool_use
from ..providers.router import get_model_provider
from ..runtime.execution import execution_workspace
from ..runtime.events import (
    log_permission_denied,
    log_tool_call_ended,
    log_tool_call_started,
)
from .basic import call_tool_handler, run_bash, run_edit, run_glob, run_read, run_write

# ── Subagent Tool ──

SUB_SYSTEM = (
    f"You are a coding subagent at {WORKDIR}. "
    "Complete the task, then return a concise final summary. "
    "Do not spawn more agents."
)


def _subagent_system_prompt() -> str:
    workspace = execution_workspace(WORKDIR)
    if workspace == WORKDIR.resolve():
        return SUB_SYSTEM
    return (
        f"You are a coding subagent at {workspace}. "
        "Complete the task, then return a concise final summary. "
        "Do not spawn more agents."
    )


SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"},
                                     "offset": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
]


SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read,
    "write_file": run_write, "edit_file": run_edit,
    "glob": run_glob,
}


def spawn_subagent(description: str) -> str:
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = get_model_provider().complete(
            model=MODEL, system=_subagent_system_prompt(), messages=messages,
            tools=SUB_TOOLS, max_tokens=8000,
            timeout=REQUEST_TIMEOUT_SECONDS)
        messages.append({"role": "assistant", "content": response.content})
        if not has_tool_use(response.content):
            break
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            log_tool_call_started(block)
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                output = str(blocked)
                log_permission_denied(block, output)
                log_tool_call_ended(block, output, "denied")
            else:
                handler = SUB_HANDLERS.get(block.name)
                output = call_tool_handler(handler, block.input, block.name)
                trigger_hooks("PostToolUse", block, output)
                log_tool_call_ended(block, output)
            results.append({"type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output)})
        messages.append({"role": "user", "content": results})
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            text = extract_text(msg["content"])
            if text:
                return text
    return "Subagent finished without a text summary."

