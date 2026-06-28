from datetime import datetime

from ..config import WORKDIR
from ..runtime.execution import execution_workspace
from .skills import list_skills
from .store import memory_injection_text

MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"


def get_mcp_clients():
    from ..mcp.client import mcp_clients_snapshot
    return mcp_clients_snapshot()


def get_active_teammates():
    from ..teams import active_teammates_snapshot
    return active_teammates_snapshot()

# ── Prompt Assembly ──

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, edit_file, glob, "
             "todo_write, todo_read, task, load_skill, memory_read, "
             "memory_append, compact, "
             "create_task, list_tasks, get_task, claim_task, complete_task, "
             "schedule_cron, list_crons, cancel_cron, "
             "spawn_teammate, send_message, check_inbox, team_status, "
             "request_shutdown, request_plan, review_plan, "
             "create_worktree, remove_worktree, keep_worktree, "
             "connect_mcp. MCP tools are prefixed mcp__{server}__{tool}.",
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    # The system prompt is rebuilt each turn from live context. This is where
    # memory, skill catalog, MCP state, and active teammates become visible.
    workspace = execution_workspace(WORKDIR)
    sections = [
        PROMPT_SECTIONS["identity"],
        PROMPT_SECTIONS["tools"],
        f"Working directory: {workspace}",
    ]
    sections.append(f"Current time: {datetime.now().isoformat(timespec='seconds')}")
    sections.append("Skills catalog:\n" + list_skills() +
                    "\nUse load_skill(name) when a skill is relevant.")
    if context.get("memories"):
        sections.append(f"Relevant memories:\n{context['memories']}")
    mcp_names = list(get_mcp_clients().keys())
    if mcp_names:
        sections.append(f"Connected MCP servers: {', '.join(mcp_names)}")
    return "\n\n".join(sections)



def update_context(context: dict, messages: list) -> dict:
    memories = memory_injection_text(execution_workspace(WORKDIR))
    return {
        "memories": memories,
        "connected_mcp": list(get_mcp_clients().keys()),
        "active_teammates": list(get_active_teammates().keys()),
    }
