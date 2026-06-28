from dataclasses import dataclass

from ..config import (
    CONTINUATION_PROMPT,
    DEFAULT_MAX_TOKENS,
    ESCALATED_MAX_TOKENS,
    MAX_RECOVERY_RETRIES,
)
from ..context_compaction import compact_history, reactive_compact
from ..cron_scheduler import consume_cron_queue
from ..hooks import trigger_hooks
from ..message_utils import (
    INTERNAL_MESSAGE_KEY,
    has_tool_use,
    internal_user_message,
)
from ..recovery import RecoveryState, is_prompt_too_long_error
from ..tools.basic import call_tool_handler
from ..tools.registry import assemble_tool_pool
from .context import (
    build_user_content,
    inject_background_notifications,
    prepare_context,
    refresh_context,
)
from .events import (
    log_final_stop,
    log_permission_denied,
    log_tool_call_ended,
    log_tool_call_started,
    scrub_sensitive_text,
)
from .llm import call_llm
from ..background import (
    has_outstanding_background_tasks,
    should_run_background,
    start_background_task,
    wait_for_background_task_update,
)
from .cancellation import raise_if_cancelled


def _tool_result_count(messages: list) -> int:
    count = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            count += sum(1 for item in content
                         if isinstance(item, dict)
                         and item.get("type") == "tool_result")
    return count


def _log_final_stop(reason: str, messages: list, **extra):
    log_final_stop(
        reason,
        message_count=len(messages),
        tool_result_count=_tool_result_count(messages),
        **extra,
    )


@dataclass(frozen=True)
class LoopOutcome:
    status: str
    reason: str
    error: str | None = None


class AgentLoop:
    """Stateful executor for one long-running agent conversation."""

    def __init__(self):
        self.rounds_since_todo = 0

    def run(self, messages: list, context: dict):
        tools, handlers = assemble_tool_pool()
        state = RecoveryState()
        max_tokens = DEFAULT_MAX_TOKENS

        while True:
            raise_if_cancelled()
            # One cycle: inject scheduled/background work, prepare context,
            # call the model, execute tool_use blocks, append tool_results,
            # repeat.
            fired = consume_cron_queue()
            for job in fired:
                messages.append(internal_user_message(
                    f"[Scheduled] {job.prompt}"
                ))
                print(f"  \033[35m[cron inject] {job.prompt[:60]}\033[0m")

            inject_background_notifications(messages)

            if self.rounds_since_todo >= 3:
                messages.append(internal_user_message(
                    "<reminder>Update your todos.</reminder>"
                ))
                self.rounds_since_todo = 0

            prepare_context(messages)
            context = refresh_context(context, messages)
            tools, handlers = assemble_tool_pool()

            try:
                response = call_llm(messages, context, tools, state, max_tokens)
            except Exception as e:
                if (is_prompt_too_long_error(e)
                        and not state.has_attempted_reactive_compact):
                    messages[:] = reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    continue
                safe_error = scrub_sensitive_text(
                    f"{type(e).__name__}: {e}"
                )
                messages.append({"role": "assistant", "content": [
                    {"type": "text", "text": f"[Error] {safe_error}"}]})
                _log_final_stop("llm_error", messages,
                                error_type=type(e).__name__)
                return LoopOutcome(
                    status="failed",
                    reason="llm_error",
                    error=safe_error,
                )
            raise_if_cancelled()

            if response.stop_reason == "max_tokens":
                if not state.has_escalated:
                    max_tokens = ESCALATED_MAX_TOKENS
                    state.has_escalated = True
                    print(f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m")
                    continue
                messages.append({"role": "assistant", "content": response.content})
                if state.recovery_count < MAX_RECOVERY_RETRIES:
                    messages.append(internal_user_message(CONTINUATION_PROMPT))
                    state.recovery_count += 1
                    continue
                _log_final_stop("max_tokens", messages)
                return LoopOutcome(
                    status="failed",
                    reason="max_tokens",
                    error="Model output exceeded the recovery token limit.",
                )

            max_tokens = DEFAULT_MAX_TOKENS
            state.has_escalated = False
            if not has_tool_use(response.content):
                if has_outstanding_background_tasks():
                    # A model may try to finish after receiving the
                    # background-start placeholder. Keep that turn in runtime
                    # context but out of user-visible history, wait for the
                    # scoped result, then let the model produce the real final
                    # answer from that result.
                    messages.append({
                        "role": "assistant",
                        "content": response.content,
                        INTERNAL_MESSAGE_KEY: True,
                    })
                    while wait_for_background_task_update():
                        raise_if_cancelled()
                    raise_if_cancelled()
                    inject_background_notifications(messages)
                    continue
                messages.append({
                    "role": "assistant",
                    "content": response.content,
                })
                trigger_hooks("Stop", messages)
                _log_final_stop(getattr(response, "stop_reason", "stop"), messages)
                return LoopOutcome(
                    status="completed",
                    reason=str(getattr(response, "stop_reason", "stop")),
                )

            messages.append({"role": "assistant", "content": response.content})
            results = []
            compacted_now = False
            for block in response.content:
                if block.type != "tool_use":
                    continue
                raise_if_cancelled()
                log_tool_call_started(block)
                print(f"\033[36m> {block.name}\033[0m")

                if block.name == "compact":
                    messages[:] = compact_history(messages)
                    messages.append(internal_user_message(
                        "[Compacted. Continue with summarized context.]"
                    ))
                    log_tool_call_ended(block, "context compacted", "compacted")
                    compacted_now = True
                    break

                blocked = trigger_hooks("PreToolUse", block)
                raise_if_cancelled()
                if blocked:
                    reason = str(blocked)
                    log_permission_denied(block, reason)
                    log_tool_call_ended(block, reason, "denied")
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": reason})
                    continue

                if should_run_background(block.name, block.input):
                    bg_id = start_background_task(block, handlers)
                    output = (f"[Background task {bg_id} started] "
                              "Result will arrive as a task_notification.")
                    log_tool_call_ended(block, output, "background_started")
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": output})
                    continue

                handler = handlers.get(block.name)
                try:
                    output = call_tool_handler(handler, block.input, block.name)
                except Exception as e:
                    log_tool_call_ended(
                        block, f"{type(e).__name__}: {e}", "failed")
                    _log_final_stop(
                        "tool_error",
                        messages,
                        tool=getattr(block, "name", None),
                        tool_use_id=getattr(block, "id", None),
                        error_type=type(e).__name__,
                    )
                    raise
                trigger_hooks("PostToolUse", block, output)
                raise_if_cancelled()
                log_tool_call_ended(block, output)
                print(str(output)[:300])

                if block.name == "todo_write":
                    self.rounds_since_todo = 0
                else:
                    self.rounds_since_todo += 1

                results.append({"type": "tool_result",
                                "tool_use_id": block.id, "content": output})

            if compacted_now:
                continue

            messages.append({"role": "user", "content": build_user_content(results)})


_DEFAULT_LOOP = AgentLoop()


def agent_loop(messages: list, context: dict):
    return _DEFAULT_LOOP.run(messages, context)
