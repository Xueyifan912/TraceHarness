import json
import re
import threading
import time
from contextvars import copy_context
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

from .config import MODEL, REQUEST_TIMEOUT_SECONDS, WORKDIR, terminal_print
from .hooks import (
    trigger_hooks,
    use_permission_resolver,
    web_child_permission_resolver,
)
from .message_utils import has_tool_use
from .providers.router import get_model_provider
from .runtime.events import (
    event_context,
    log_event,
    log_permission_denied,
    log_tool_call_ended,
    log_tool_call_started,
    safe_text_preview,
)
from .runtime.execution import (
    current_execution_context,
    execution_context,
    execution_workspace,
)
from .runtime.fileio import (
    append_text_locked,
    exclusive_file_lock,
    safe_runtime_path,
)
from .task_system.tasks import (
    can_start,
    claim_task,
    complete_task,
    format_task_line,
    list_tasks,
    load_task,
    tasks_dir,
)
from .task_system.worktrees import (
    format_worktree_status,
    list_worktree_statuses,
    worktrees_dir,
)
from .tools.basic import call_tool_handler, run_bash, run_read, run_write

# ── MessageBus ──

# Team communication is append-only JSONL mailboxes. This keeps the protocol
# inspectable on disk and lets background teammates send messages.
TASKS_DIR = WORKDIR / ".tasks"
WORKTREES_DIR = WORKDIR / ".worktrees"
MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)
VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAILBOX_LOCK = threading.Lock()


def _validate_agent_name(agent: str) -> str:
    if not isinstance(agent, str) or not agent:
        raise ValueError("Agent name cannot be empty")
    if agent in (".", "..") or not VALID_AGENT_NAME.fullmatch(agent):
        raise ValueError(f"Invalid agent name: {agent!r}")
    return agent


def _team_tasks_dir() -> Path:
    if current_execution_context().get("workspace"):
        return tasks_dir()
    path = Path(TASKS_DIR).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _team_worktrees_dir() -> Path:
    if current_execution_context().get("workspace"):
        return worktrees_dir()
    path = Path(WORKTREES_DIR).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _mailbox_dir() -> Path:
    if current_execution_context().get("workspace"):
        path = execution_workspace(WORKDIR) / ".mailboxes"
    else:
        configured = Path(MAILBOX_DIR)
        default = Path(WORKDIR) / ".mailboxes"
        if configured == default:
            return safe_runtime_path(
                WORKDIR,
                ".mailboxes",
                create_directory=True,
            )
        path = configured.resolve()
    return safe_runtime_path(
        path.parent,
        path.name,
        create_directory=True,
    )


def _mailbox_path(agent: str) -> Path:
    safe_agent = _validate_agent_name(agent)
    base = _mailbox_dir().resolve()
    path = (base / f"{safe_agent}.jsonl").resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Mailbox path escapes mailbox directory: {agent!r}")
    return path


def _content_meta(content: str, limit: int = 240) -> dict:
    return safe_text_preview(content, limit=limit)


def _audit_message_sent(msg: dict):
    meta = msg.get("metadata") or {}
    log_event("teammate_message_sent", {
        "from": msg.get("from"),
        "to": msg.get("to"),
        "type": msg.get("type"),
        "metadata_keys": sorted(meta.keys()),
        "request_id": meta.get("request_id"),
        "approve": meta.get("approve"),
        "content": _content_meta(msg.get("content", "")),
    })


class MessageBus:
    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict = None):
        _validate_agent_name(from_agent)
        inbox = _mailbox_path(to_agent)
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        inbox.parent.mkdir(parents=True, exist_ok=True)
        with _MAILBOX_LOCK:
            append_text_locked(
                inbox,
                json.dumps(msg, ensure_ascii=False) + "\n",
            )
        _audit_message_sent(msg)
        terminal_print(f"  \033[33m[bus] {from_agent} -> {to_agent}: "
                       f"({msg_type}) {content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list[dict]:
        inbox = _mailbox_path(agent)
        processing = inbox.with_name(
            f"{inbox.stem}.processing-{time.time_ns()}-"
            f"{threading.get_ident()}{inbox.suffix}"
        )
        msgs: list[dict] = []
        corrupt_lines: list[str] = []
        mailbox_lock_path = inbox.with_name(f".{inbox.name}.lock")
        with _MAILBOX_LOCK, exclusive_file_lock(mailbox_lock_path):
            candidates = sorted(
                inbox.parent.glob(
                    f"{inbox.stem}.processing-*{inbox.suffix}"
                )
            )
            if inbox.exists():
                inbox.replace(processing)
                candidates.append(processing)
            if not candidates:
                return []
            for candidate in candidates:
                try:
                    lines = candidate.read_text(
                        encoding="utf-8"
                    ).splitlines()
                except Exception:
                    continue
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        parsed = json.loads(line)
                    except Exception:
                        corrupt_lines.append(line)
                        continue
                    if isinstance(parsed, dict):
                        msgs.append(parsed)
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
            if corrupt_lines:
                corrupt_path = inbox.with_name(
                    f"{inbox.stem}.corrupt-{time.time_ns()}{inbox.suffix}"
                )
                corrupt_path.write_text(
                    "\n".join(corrupt_lines) + "\n",
                    encoding="utf-8",
                )
        log_event("teammate_messages_read", {
            "agent": agent,
            "count": len(msgs),
            "types": [msg.get("type") for msg in msgs],
        })
        return msgs


BUS = MessageBus()
active_teammates: dict[str, dict] = {}
_TEAM_STATE_LOCK = threading.RLock()

# ── Protocol State ──

@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    created_at: float = field(default_factory=time.time)
    workspace: str | None = None


pending_requests: dict[str, ProtocolState] = {}


def _current_workspace_key() -> str | None:
    workspace = current_execution_context().get("workspace")
    if workspace is None:
        return None
    return str(Path(workspace).resolve())


def active_teammates_snapshot() -> dict[str, dict]:
    with _TEAM_STATE_LOCK:
        return {
            name: dict(info) if isinstance(info, dict) else info
            for name, info in active_teammates.items()
        }


def pending_requests_snapshot() -> dict[str, ProtocolState]:
    with _TEAM_STATE_LOCK:
        return {
            request_id: ProtocolState(**asdict(request))
            for request_id, request in pending_requests.items()
        }


def _prune_protocol_states_locked(max_resolved: int = 500) -> None:
    resolved = sorted(
        (
            request
            for request in pending_requests.values()
            if request.status != "pending"
        ),
        key=lambda request: request.created_at,
    )
    for request in resolved[:-max_resolved]:
        pending_requests.pop(request.request_id, None)


def _update_active_teammate(name: str, **updates):
    with _TEAM_STATE_LOCK:
        entry = active_teammates.get(name)
        if not isinstance(entry, dict):
            return
        entry.update(updates)


def _teammate_matches_workspace_locked(name: str) -> bool:
    current_workspace = _current_workspace_key()
    if current_workspace is None:
        return True
    entry = active_teammates.get(name)
    if not isinstance(entry, dict):
        return True
    teammate_workspace = entry.get("workspace")
    return teammate_workspace in (None, current_workspace)


def _workspace_visible(workspace: str | None, current_workspace: str | None) -> bool:
    return current_workspace is None or workspace in (None, current_workspace)


def _task_context_from_id(task_id: str) -> dict:
    try:
        task = load_task(task_id)
    except Exception:
        return {"task_id": task_id}
    return {
        "task_id": task.id,
        "task_subject": task.subject,
        "task_status": task.status,
        "task_owner": task.owner,
        "worktree": task.worktree,
        "worktree_path": (
            str(_team_worktrees_dir() / task.worktree)
            if task.worktree else None
        ),
    }


def _format_teammate_handoff(
    name: str,
    role: str,
    context: dict,
    summary: str,
) -> str:
    lines = [f"Teammate: {name}", f"Role: {role}"]
    task_id = context.get("task_id")
    if task_id:
        subject = context.get("task_subject") or "(unknown subject)"
        status = context.get("task_status") or "unknown"
        owner = context.get("task_owner") or "unassigned"
        lines.append(f"Task: {task_id} - {subject} [{status}] owner={owner}")
    worktree = context.get("worktree")
    worktree_path = context.get("worktree_path")
    if worktree or worktree_path:
        lines.append(
            f"Worktree: {worktree or '(unknown)'}"
            + (f" at {worktree_path}" if worktree_path else "")
        )
    lines.append("Summary:")
    lines.append(summary or "(no summary)")
    return "\n".join(lines)


def team_status() -> str:
    current_workspace = _current_workspace_key()
    teammates = active_teammates_snapshot()
    teammates = {
        name: info
        for name, info in teammates.items()
        if (
            not isinstance(info, dict) or
            _workspace_visible(info.get("workspace"), current_workspace)
        )
    }
    requests = pending_requests_snapshot()
    requests = {
        request_id: request
        for request_id, request in requests.items()
        if _workspace_visible(request.workspace, current_workspace)
    }
    lines = ["Active teammates:"]
    if teammates:
        for name, info in sorted(teammates.items()):
            if isinstance(info, dict):
                role = info.get("role", "unknown")
                status = info.get("status", "running")
                task = info.get("task_id") or "none"
                worktree = info.get("worktree") or "none"
                path = info.get("worktree_path") or "none"
            else:
                role, status, task, worktree, path = (
                    "unknown", "running", "none", "none", "none")
            lines.append(
                f"- {name} | role={role} | status={status} "
                f"| task={task} | worktree={worktree} | path={path}"
            )
    else:
        lines.append("- none")

    lines.append("Pending protocol requests:")
    if requests:
        for req_id, request in sorted(requests.items()):
            lines.append(
                f"- {req_id} | type={request.type} | from={request.sender} "
                f"| to={request.target} | status={request.status}"
            )
    else:
        lines.append("- none")

    lines.append("Tasks:")
    tasks = list_tasks()
    if tasks:
        lines.extend(f"- {format_task_line(task)}" for task in tasks)
    else:
        lines.append("- none")

    lines.append("Worktrees:")
    worktrees = list_worktree_statuses()
    if worktrees:
        lines.extend(f"- {format_worktree_status(status)}"
                     for status in worktrees)
    else:
        lines.append("- none")
    return "\n".join(lines)


def new_request_id() -> str:
    with _TEAM_STATE_LOCK:
        while True:
            request_id = f"req_{uuid4().hex}"
            if request_id not in pending_requests:
                return request_id


def match_response(response_type: str, request_id: str, approve: bool):
    # Responses are matched by request_id so one protocol reply cannot approve
    # a different pending request.
    with _TEAM_STATE_LOCK:
        state = pending_requests.get(request_id)
        if not state:
            return
        if state.type == "shutdown" and response_type != "shutdown_response":
            return
        if (
            state.type == "plan_approval"
            and response_type != "plan_approval_response"
        ):
            return
        if state.status != "pending":
            return
        state.status = "approved" if approve else "rejected"
        _prune_protocol_states_locked()


def consume_lead_inbox(route_protocol=True) -> list[dict]:
    msgs = BUS.read_inbox("lead")
    if route_protocol:
        for msg in msgs:
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            msg_type = msg.get("type", "")
            if req_id and msg_type.endswith("_response"):
                match_response(msg_type, req_id, meta.get("approve", False))
    return msgs


# ── Autonomous Agent ──

IDLE_POLL_INTERVAL = 5
IDLE_TIMEOUT = 60


def scan_unclaimed_tasks() -> list[dict]:
    unclaimed = []
    for task_record in list_tasks():
        task = asdict(task_record)
        if (
            task_record.status == "pending"
            and not task_record.owner
            and can_start(task_record.id)
        ):
            unclaimed.append(task)
    return unclaimed


def idle_poll(agent_name: str, messages: list,
              name: str, role: str,
              worktree_context: dict | None = None) -> str:
    # Autonomous teammates wake up for inbox messages first, then look for
    # unclaimed tasks. This keeps direct protocol messages higher priority.
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    req_id = msg.get("metadata", {}).get("request_id", "")
                    BUS.send(name, "lead", "Shutting down.",
                             "shutdown_response",
                             {"request_id": req_id, "approve": True})
                    return "shutdown"
            messages.append({"role": "user",
                "content": "<inbox>" + json.dumps(inbox) + "</inbox>"})
            return "work"
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task_data = unclaimed[0]
            result = claim_task(task_data["id"], agent_name)
            if "Claimed" in result:
                context_updates = {
                    "status": "working",
                    "task_id": task_data["id"],
                    "task_subject": task_data.get("subject"),
                    "task_status": "in_progress",
                    "task_owner": agent_name,
                    "worktree": task_data.get("worktree"),
                }
                wt_info = ""
                if task_data.get("worktree"):
                    wt_path = _team_worktrees_dir() / task_data["worktree"]
                    wt_info = f"\nWork directory: {wt_path}"
                    context_updates["worktree_path"] = str(wt_path)
                    if worktree_context is not None:
                        worktree_context["path"] = str(wt_path)
                if worktree_context is not None:
                    worktree_context.update(context_updates)
                _update_active_teammate(agent_name, **context_updates)
                messages.append({"role": "user",
                    "content": f"<auto-claimed>Task {task_data['id']}: "
                               f"{task_data['subject']}{wt_info}</auto-claimed>"})
                return "work"
    return "timeout"


# ── Teammate Thread ──

def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    try:
        _validate_agent_name(name)
    except ValueError as exc:
        return f"Error: {exc}"
    # Plan approval is a real gate: after submit_plan, the teammate stops
    # taking model/tool steps until lead sends plan_approval_response.
    protocol_ctx = {"waiting_plan": None}
    system = (f"You are '{name}', a {role}. "
              f"Working directory: {execution_workspace(WORKDIR)}. "
              f"Use tools to complete tasks. "
              f"If a task has a worktree, work in that directory.")

    def handle_inbox_message(name: str, msg: dict, messages: list):
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")
        if msg_type == "shutdown_request":
            BUS.send(name, "lead", "Shutting down.",
                     "shutdown_response",
                     {"request_id": req_id, "approve": True})
            return True
        if msg_type == "plan_approval_response":
            approve = meta.get("approve", False)
            if req_id == protocol_ctx["waiting_plan"]:
                protocol_ctx["waiting_plan"] = None
                _update_active_teammate(name, status="running",
                                        pending_request=None)
            messages.append({"role": "user",
                "content": "[Plan approved]" if approve
                           else f"[Plan rejected] {msg['content']}"})
        return False

    def run():
        wt_ctx = {
            "path": None,
            "task_id": None,
            "task_subject": None,
            "task_status": None,
            "task_owner": None,
            "worktree": None,
            "worktree_path": None,
        }

        def _wt_cwd():
            # Once a task with a worktree is claimed, all teammate file tools
            # transparently run inside that isolated directory.
            p = wt_ctx["path"]
            return Path(p) if p else None

        def _run_read(
            path: str,
            limit: int | None = None,
            offset: int = 0,
        ) -> str:
            return run_read(
                path,
                limit=limit,
                offset=offset,
                cwd=_wt_cwd(),
            )

        def _run_write(path: str, content: str) -> str:
            return run_write(path, content, cwd=_wt_cwd())

        def _run_list_tasks():
            tasks = list_tasks()
            if not tasks:
                return "No tasks."
            return "\n".join(
                f"  {format_task_line(t)}"
                for t in tasks)

        def _run_send_message(to: str, content: str) -> str:
            try:
                BUS.send(name, to, content)
            except ValueError as exc:
                return f"Error: {exc}"
            return "Sent"

        def _run_claim_task(task_id: str):
            try:
                result = claim_task(task_id, owner=name)
            except (FileNotFoundError, ValueError) as exc:
                return f"Error: {exc}"
            if "Claimed" in result:
                context_updates = _task_context_from_id(task_id)
                context_updates["status"] = "working"
                context_updates["task_status"] = "in_progress"
                context_updates["task_owner"] = name
                wt_ctx.update(context_updates)
                wt_ctx["path"] = context_updates.get("worktree_path")
                if wt_ctx["path"]:
                    wt_ctx["worktree_path"] = wt_ctx["path"]
                _update_active_teammate(name, **context_updates)
            return result

        def _run_complete_task(task_id: str):
            try:
                result = complete_task(task_id, owner=name)
            except (FileNotFoundError, ValueError) as exc:
                return f"Error: {exc}"
            if "Completed" in result:
                wt_ctx.update(_task_context_from_id(task_id))
                wt_ctx["task_status"] = "completed"
                _update_active_teammate(name, status="completed",
                                        **_task_context_from_id(task_id))
            wt_ctx["path"] = None
            return result

        messages = [{"role": "user", "content": prompt}]
        sub_tools = [
            {"name": "read_file", "description": "Read file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "limit": {"type": "integer"},
                                             "offset": {"type": "integer"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "send_message",
             "description": "Send message to another agent.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
            {"name": "submit_plan",
             "description": "Submit a plan for Lead approval.",
             "input_schema": {"type": "object",
                              "properties": {"plan": {"type": "string"}},
                              "required": ["plan"]}},
            {"name": "list_tasks",
             "description": "List all tasks.",
             "input_schema": {"type": "object", "properties": {},
                              "required": []}},
            {"name": "claim_task",
             "description": "Claim a pending task.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
            {"name": "complete_task",
             "description": "Mark an in-progress task as completed.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
        ]

        sub_handlers = {
            "read_file": _run_read,
            "write_file": _run_write,
            "send_message": _run_send_message,
            "list_tasks": _run_list_tasks,
            "claim_task": _run_claim_task,
            "complete_task": _run_complete_task,
        }

        while True:
            if len(messages) <= 3:
                messages.insert(0, {"role": "user",
                    "content": f"<identity>You are '{name}', role: {role}. "
                               f"Continue your work.</identity>"})
            should_shutdown = False
            for _ in range(10):
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    stopped = handle_inbox_message(name, msg, messages)
                    if stopped:
                        should_shutdown = True
                        break
                if should_shutdown:
                    break
                if protocol_ctx["waiting_plan"]:
                    # Poll only for protocol replies while the approval gate is
                    # closed; do not let the model continue with the task.
                    time.sleep(IDLE_POLL_INTERVAL)
                    continue
                if inbox and not should_shutdown:
                    non_protocol = [m for m in inbox
                                    if m.get("type") == "message"]
                    if non_protocol:
                        messages.append({"role": "user",
                            "content": "<inbox>" + json.dumps(non_protocol) + "</inbox>"})
                try:
                    response = get_model_provider().complete(
                        model=MODEL, system=system, messages=messages[-20:],
                        tools=sub_tools, max_tokens=8000,
                        timeout=REQUEST_TIMEOUT_SECONDS)
                except Exception:
                    break
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
                    elif block.name == "submit_plan":
                        output = _teammate_submit_plan(
                            name, block.input.get("plan", ""))
                        trigger_hooks("PostToolUse", block, output)
                        match = re.search(r"\((req_[0-9a-f]+)\)", output)
                        protocol_ctx["waiting_plan"] = (
                            match.group(1) if match else output)
                        _update_active_teammate(
                            name,
                            status="waiting_plan",
                            pending_request=protocol_ctx["waiting_plan"],
                        )
                        log_tool_call_ended(block, output)
                    else:
                        handler = sub_handlers.get(block.name)
                        output = call_tool_handler(handler, block.input,
                                                   block.name)
                        trigger_hooks("PostToolUse", block, output)
                        log_tool_call_ended(block, output)
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": str(output)})
                    if protocol_ctx["waiting_plan"]:
                        # Ignore later tool_use blocks from the same model
                        # response; they belong after approval, not before.
                        break
                messages.append({"role": "user", "content": results})
                if protocol_ctx["waiting_plan"]:
                    break
            if should_shutdown:
                break
            if protocol_ctx["waiting_plan"]:
                continue
            idle_result = idle_poll(name, messages, name, role, wt_ctx)
            if idle_result in ("shutdown", "timeout"):
                break

        summary = "Done."
        for msg in reversed(messages):
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                for b in msg["content"]:
                    if getattr(b, "type", None) == "text":
                        summary = b.text
                        break
                else:
                    continue
                break
        wt_ctx.update(_task_context_from_id(wt_ctx["task_id"])
                      if wt_ctx.get("task_id") else {})
        handoff = _format_teammate_handoff(name, role, wt_ctx, summary)
        BUS.send(name, "lead", handoff, "result", {
            "role": role,
            "task_id": wt_ctx.get("task_id") or "",
            "worktree": wt_ctx.get("worktree") or "",
        })
    with _TEAM_STATE_LOCK:
        if name in active_teammates:
            return f"Teammate '{name}' already exists"
        active_teammates[name] = {
            "role": role,
            "status": "running",
            "started_at": time.time(),
            "prompt_preview": prompt[:120],
            "task_id": None,
            "worktree": None,
            "worktree_path": None,
            "workspace": _current_workspace_key(),
        }
    worker_context = copy_context()

    def run_worker():
        # Teammates are autonomous after the spawn tool returns. Preserve
        # workspace/session ownership, but do not emit late child activity as
        # if it still belonged to the parent run after that run terminates.
        try:
            with execution_context(
                detached=True,
                clear_run_id=True,
            ), event_context(clear_run_id=True):
                context = current_execution_context()
                if context.get("source") == "web":
                    with use_permission_resolver(web_child_permission_resolver):
                        run()
                else:
                    run()
        finally:
            with _TEAM_STATE_LOCK:
                active_teammates.pop(name, None)

    worker = threading.Thread(
        target=lambda: worker_context.run(run_worker),
        daemon=True,
    )
    try:
        worker.start()
    except Exception as exc:
        with _TEAM_STATE_LOCK:
            active_teammates.pop(name, None)
        return f"Error: failed to spawn teammate: {exc}"
    return f"Teammate '{name}' spawned as {role}"


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    req_id = new_request_id()
    with _TEAM_STATE_LOCK:
        pending_requests[req_id] = ProtocolState(
            request_id=req_id, type="plan_approval",
            sender=from_name, target="lead",
            status="pending", payload=plan,
            workspace=_current_workspace_key())
    try:
        BUS.send(from_name, "lead", plan,
                 "plan_approval_request",
                 {"request_id": req_id})
    except Exception:
        with _TEAM_STATE_LOCK:
            pending_requests.pop(req_id, None)
        raise
    return f"Plan submitted ({req_id})"


# ── Lead Protocol Tools ──

def run_request_shutdown(teammate: str) -> str:
    req_id = new_request_id()
    with _TEAM_STATE_LOCK:
        if not _teammate_matches_workspace_locked(teammate):
            return f"Teammate {teammate} not found"
        pending_requests[req_id] = ProtocolState(
            request_id=req_id, type="shutdown",
            sender="lead", target=teammate,
            status="pending", payload="",
            workspace=_current_workspace_key())
    try:
        BUS.send("lead", teammate, "Shut down.", "shutdown_request",
                 {"request_id": req_id})
    except Exception:
        with _TEAM_STATE_LOCK:
            pending_requests.pop(req_id, None)
        raise
    return f"Shutdown request sent to {teammate}"


def run_request_plan(teammate: str, task: str) -> str:
    with _TEAM_STATE_LOCK:
        if not _teammate_matches_workspace_locked(teammate):
            return f"Teammate {teammate} not found"
    BUS.send("lead", teammate, f"Submit plan for: {task}", "message")
    return f"Asked {teammate} to submit a plan"


def run_review_plan(request_id: str, approve: bool,
                    feedback: str = "") -> str:
    with _TEAM_STATE_LOCK:
        state = pending_requests.get(request_id)
        if not state:
            return f"Request {request_id} not found"
        current_workspace = _current_workspace_key()
        if (
            current_workspace is not None and
            state.workspace is not None and
            state.workspace != current_workspace
        ):
            return f"Request {request_id} not found"
        if state.type != "plan_approval":
            return f"Request {request_id} is not a plan approval"
        if state.status != "pending":
            return f"Request {request_id} is already {state.status}"
        state.status = "approved" if approve else "rejected"
        sender = state.sender
        _prune_protocol_states_locked()
    try:
        BUS.send("lead", sender,
                 feedback or ("Approved" if approve else "Rejected"),
                 "plan_approval_response",
                 {"request_id": request_id, "approve": approve})
    except Exception:
        with _TEAM_STATE_LOCK:
            current = pending_requests.get(request_id)
            if current is state:
                current.status = "pending"
        raise
    return f"Plan {'approved' if approve else 'rejected'}"

