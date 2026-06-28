"""Backward-compatible entry points for the runtime package.

The original teaching harness kept CLI, context preparation, LLM calls, and
tool dispatch in this single file. The implementation now lives under
`coding_agent.runtime`, while this module preserves the public imports used by
`main.py` and any existing tests or scripts.
"""

from .runtime.cli import agent_lock, cron_autorun_loop, run_cli
from .runtime.context import (
    build_user_content,
    inject_background_notifications,
    prepare_context,
)
from .runtime.display import print_turn_assistants
from .runtime.llm import call_llm
from .runtime.loop import AgentLoop, agent_loop

__all__ = [
    "AgentLoop",
    "agent_lock",
    "agent_loop",
    "build_user_content",
    "call_llm",
    "cron_autorun_loop",
    "inject_background_notifications",
    "prepare_context",
    "print_turn_assistants",
    "run_cli",
]
