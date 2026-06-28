import json
import re
import time
from pathlib import Path
from uuid import uuid4

from .config import (
    KEEP_RECENT_TOOL_RESULTS,
    MODEL,
    PERSIST_THRESHOLD,
    REQUEST_TIMEOUT_SECONDS,
    WORKDIR,
)
from .message_utils import extract_text, internal_user_message
from .providers.router import get_model_provider
from .runtime.execution import current_execution_context, execution_workspace
from .runtime.fileio import atomic_write_text, safe_runtime_path

# ── Context Compaction ──

# Compaction is layered: first shrink oversized tool results, then trim old
# message ranges, and only call the model for a summary when the context is
# still too large or the model explicitly asks for compact.
def estimate_size(messages: list) -> int:
    return len(json.dumps(messages, default=str))

def block_type(block):
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def message_has_tool_use(message: dict) -> bool:
    if message.get("role") != "assistant":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(block_type(block) == "tool_use" for block in content)


def is_tool_result_message(message: dict) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result"
               for block in content)


def collect_tool_results(messages: list):
    found = []
    for mi, msg in enumerate(messages):
        content = msg.get("content")
        if msg.get("role") != "user" or not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                found.append((mi, bi, block))
    return found


def _safe_identifier(value: object, fallback: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or ""))
    safe = safe.strip("._-")
    return safe[:80] or fallback


def _tool_results_dir() -> Path:
    return safe_runtime_path(
        execution_workspace(WORKDIR),
        ".task_outputs",
        "tool-results",
        create_directory=True,
    )


def _transcript_dir() -> Path:
    return safe_runtime_path(
        execution_workspace(WORKDIR),
        ".transcripts",
        create_directory=True,
    )


def persist_large_output(tool_use_id: str, output: str) -> str:
    if len(output) <= PERSIST_THRESHOLD:
        return output
    context = current_execution_context()
    session = _safe_identifier(context.get("session_id"), "session")
    run = _safe_identifier(context.get("run_id"), "run")
    tool = _safe_identifier(tool_use_id, "tool")
    path = _tool_results_dir() / f"{session}-{run}-{tool}.txt"
    if not path.exists():
        atomic_write_text(path, output)
    return (f"<persisted-output>\nFull output: {path}\n"
            f"Preview:\n{output[:2000]}\n</persisted-output>")


def tool_result_budget(messages: list, max_bytes: int = 200_000) -> list:
    if not messages:
        return messages
    last = messages[-1]
    content = last.get("content")
    if last.get("role") != "user" or not isinstance(content, list):
        return messages
    blocks = [(i, b) for i, b in enumerate(content)
              if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= max_bytes:
        return messages
    for _, block in sorted(blocks,
                           key=lambda pair: len(str(pair[1].get("content", ""))),
                           reverse=True):
        if total <= max_bytes:
            break
        text = str(block.get("content", ""))
        block["content"] = persist_large_output(
            block.get("tool_use_id", "unknown"), text)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return messages


def snip_compact(messages: list, max_messages: int = 50) -> list:
    if len(messages) <= max_messages:
        return messages
    head_end, tail_start = 3, len(messages) - (max_messages - 3)
    if head_end > 0 and message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and is_tool_result_message(messages[head_end]):
            head_end += 1
    if (tail_start > 0 and tail_start < len(messages)
            and is_tool_result_message(messages[tail_start])
            and message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    if head_end >= tail_start:
        return messages
    snipped = tail_start - head_end
    return (messages[:head_end]
            + [internal_user_message(f"[snipped {snipped} messages]")]
            + messages[tail_start:])


def micro_compact(messages: list) -> list:
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return messages
    for _, _, block in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        if len(str(block.get("content", ""))) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


def write_transcript(messages: list) -> Path:
    context = current_execution_context()
    session = _safe_identifier(context.get("session_id"), "session")
    run = _safe_identifier(context.get("run_id"), "run")
    path = _transcript_dir() / (
        f"transcript_{session}_{run}_{time.time_ns()}_{uuid4().hex[:8]}.jsonl"
    )
    text = "".join(
        json.dumps(msg, ensure_ascii=False, default=str) + "\n"
        for msg in messages
    )
    atomic_write_text(path, text)
    return path


def summarize_history(messages: list) -> str:
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = ("Summarize this coding-agent conversation so work can continue. "
              "Preserve current goal, key findings, changed files, remaining work, "
              "and user constraints.\n\n" + conversation)
    provider = get_model_provider()
    response = provider.complete(
        model=MODEL,
        system="",
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        max_tokens=2000,
        timeout=REQUEST_TIMEOUT_SECONDS)
    return extract_text(response.content) or "(empty summary)"


def compact_history(messages: list) -> list:
    transcript = write_transcript(messages)
    print(f"  \033[36m[compact] transcript saved: {transcript}\033[0m")
    summary = summarize_history(messages)
    return [internal_user_message(f"[Compacted]\n\n{summary}")]


def reactive_compact(messages: list) -> list:
    transcript = write_transcript(messages)
    print(f"  \033[31m[reactive compact] transcript saved: {transcript}\033[0m")
    tail_start = max(0, len(messages) - 5)
    if (tail_start > 0 and tail_start < len(messages)
            and is_tool_result_message(messages[tail_start])
            and message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    try:
        summary = summarize_history(messages[:tail_start])
    except Exception:
        summary = "Earlier conversation was trimmed after a prompt-too-long error."
    return [internal_user_message(f"[Reactive compact]\n\n{summary}"),
            *messages[tail_start:]]

