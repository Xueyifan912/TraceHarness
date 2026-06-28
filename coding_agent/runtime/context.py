from ..background import collect_background_results
from ..config import CONTEXT_LIMIT
from ..context_compaction import (
    compact_history,
    estimate_size,
    micro_compact,
    reactive_compact,
    snip_compact,
    tool_result_budget,
)
from ..memory.context import update_context
from ..message_utils import internal_user_message


def prepare_context(messages: list) -> list:
    # Every LLM turn enters through the same context budget pipeline.
    messages[:] = tool_result_budget(messages)
    messages[:] = snip_compact(messages)
    messages[:] = micro_compact(messages)
    if estimate_size(messages) > CONTEXT_LIMIT:
        messages[:] = compact_history(messages)
    return messages


def refresh_context(context: dict, messages: list) -> dict:
    return update_context(context, messages)


def build_user_content(results: list[dict]) -> list[dict]:
    # Tool results and completed background notifications are both returned to
    # the model as user-side content, matching the tool_result feedback loop.
    content = list(results)
    for note in collect_background_results():
        content.append({"type": "text", "text": note})
    return content


def inject_background_notifications(messages: list):
    notes = collect_background_results()
    if notes:
        messages.append(internal_user_message([
            {"type": "text", "text": note} for note in notes]))
