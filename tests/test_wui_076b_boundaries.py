import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


def _events(workspace):
    path = workspace / ".agent_events" / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _tool_result_from_messages(messages):
    for message in reversed(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return block.get("content")
    return None


def test_web_run_file_tools_use_service_workspace(monkeypatch, tmp_path):
    from coding_agent.providers.base import ModelResponse, TextBlock, ToolUseBlock
    from coding_agent.runtime import loop as loop_mod
    from coding_agent.web.agent_service import AgentService

    marker = f"wui_076b_{uuid4().hex}.txt"
    repo_marker = Path.cwd() / marker
    assert not repo_marker.exists()
    calls = {"llm": 0}

    def fake_call_llm(messages, context, tools, state, max_tokens):
        calls["llm"] += 1
        if calls["llm"] == 1:
            return ModelResponse(
                content=[ToolUseBlock(
                    id="toolu_write",
                    name="write_file",
                    input={"path": marker, "content": "from web"},
                )],
                stop_reason="tool_use",
            )
        if calls["llm"] == 2:
            return ModelResponse(
                content=[ToolUseBlock(
                    id="toolu_read",
                    name="read_file",
                    input={"path": marker},
                )],
                stop_reason="tool_use",
            )
        return ModelResponse(
            content=[TextBlock("done")],
            stop_reason="end_turn",
        )

    monkeypatch.setattr(loop_mod, "call_llm", fake_call_llm)
    service = AgentService(workspace=tmp_path)
    session_id = service.create_session()["session"]["session_id"]

    response = service.post_message(session_id, "write then read")

    assert response["run"]["status"] == "completed"
    assert (tmp_path / marker).read_text(encoding="utf-8") == "from web"
    assert not repo_marker.exists()
    messages = service.get_session(session_id)["messages"]
    tool_results = [
        block["content"]
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert "from web" in tool_results


def test_web_run_bash_uses_service_workspace(monkeypatch, tmp_path):
    from coding_agent.providers.base import ModelResponse, TextBlock, ToolUseBlock
    from coding_agent.runtime import loop as loop_mod
    from coding_agent.tools import basic as basic_mod
    from coding_agent.web.agent_service import AgentService

    calls = {"llm": 0, "cwd": None}

    def fake_run(
        command, shell, cwd, capture_output, text, errors, timeout
    ):
        assert errors == "replace"
        calls["cwd"] = cwd
        return SimpleNamespace(returncode=0, stdout=str(cwd), stderr="")

    def fake_call_llm(messages, context, tools, state, max_tokens):
        calls["llm"] += 1
        if calls["llm"] == 1:
            return ModelResponse(
                content=[ToolUseBlock(
                    id="toolu_pwd",
                    name="bash",
                    input={"command": "pwd"},
                )],
                stop_reason="tool_use",
            )
        return ModelResponse(
            content=[TextBlock("done")],
            stop_reason="end_turn",
        )

    monkeypatch.setattr(basic_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(loop_mod, "call_llm", fake_call_llm)
    (tmp_path / ".agent_policy.yaml").write_text(
        "bash:\n  default_action: allow\n",
        encoding="utf-8",
    )
    service = AgentService(workspace=tmp_path)
    session_id = service.create_session()["session"]["session_id"]

    response = service.post_message(session_id, "pwd")

    assert response["run"]["status"] == "completed"
    assert Path(calls["cwd"]).resolve() == tmp_path.resolve()


class _SequenceProvider:
    name = "fake-provider"

    def __init__(self, first_response, final_text="done"):
        self.first_response = first_response
        self.final_text = final_text
        self.calls = 0
        self.tool_result = None

    def complete(self, **kwargs):
        from coding_agent.providers.base import TextBlock

        self.calls += 1
        if self.calls == 1:
            return self.first_response
        self.tool_result = _tool_result_from_messages(kwargs["messages"])
        return SimpleNamespace(
            content=[TextBlock(self.final_text)],
            stop_reason="end_turn",
        )


def test_subagent_denied_tool_does_not_execute(monkeypatch, tmp_path):
    from coding_agent.providers.base import ToolUseBlock
    from coding_agent.runtime.events import event_context
    from coding_agent.tools import subagent as subagent_mod

    def fail_input(*args, **kwargs):
        raise AssertionError("deny must not call input")

    calls = {"bash": 0}
    provider = _SequenceProvider(SimpleNamespace(
        content=[ToolUseBlock(
            id="toolu_sub_deny",
            name="bash",
            input={"command": "sudo reboot"},
        )],
        stop_reason="tool_use",
    ))
    monkeypatch.setattr("builtins.input", fail_input)
    monkeypatch.setattr(subagent_mod, "get_model_provider", lambda: provider)
    monkeypatch.setitem(
        subagent_mod.SUB_HANDLERS,
        "bash",
        lambda command: calls.__setitem__("bash", calls["bash"] + 1) or "ran",
    )

    with event_context(
        session_id="session_sub",
        run_id="run_sub",
        source="web",
        workspace=tmp_path,
    ):
        result = subagent_mod.spawn_subagent("try denied command")

    assert result == "done"
    assert calls["bash"] == 0
    assert "deny list" in provider.tool_result
    events = _events(tmp_path)
    assert any(event["type"] == "permission_denied" for event in events)
    assert any(
        event["type"] == "tool_call_ended"
        and event["payload"]["status"] == "denied"
        for event in events
    )


def test_subagent_web_ask_uses_web_resolver_without_input(monkeypatch, tmp_path):
    from coding_agent.providers.base import ToolUseBlock
    from coding_agent.runtime.events import event_context
    from coding_agent.tools import subagent as subagent_mod
    from coding_agent.web.approvals import (
        ApprovalRegistry,
        WEB_APPROVAL_EXPIRED_REASON,
        web_permission_context,
    )

    def fail_input(*args, **kwargs):
        raise AssertionError("web resolver must not call input")

    calls = {"bash": 0}
    provider = _SequenceProvider(SimpleNamespace(
        content=[ToolUseBlock(
            id="toolu_sub_ask",
            name="bash",
            input={"command": "rm build/output.txt"},
        )],
        stop_reason="tool_use",
    ))
    registry = ApprovalRegistry(timeout_seconds=0.01, workspace=tmp_path)
    monkeypatch.setattr("builtins.input", fail_input)
    monkeypatch.setattr(subagent_mod, "get_model_provider", lambda: provider)
    monkeypatch.setitem(
        subagent_mod.SUB_HANDLERS,
        "bash",
        lambda command: calls.__setitem__("bash", calls["bash"] + 1) or "ran",
    )

    with event_context(
        session_id="session_sub_ask",
        run_id="run_sub_ask",
        source="web",
        workspace=tmp_path,
    ), web_permission_context(registry):
        result = subagent_mod.spawn_subagent("try ask command")

    assert result == "done"
    assert calls["bash"] == 0
    assert provider.tool_result == WEB_APPROVAL_EXPIRED_REASON


class _FakeBus:
    def __init__(self):
        self.sent = []

    def send(self, from_agent, to_agent, content,
             msg_type="message", metadata=None):
        self.sent.append({
            "from": from_agent,
            "to": to_agent,
            "content": content,
            "type": msg_type,
            "metadata": metadata or {},
        })

    def read_inbox(self, agent):
        return []


def _run_teammate_bash(monkeypatch, tmp_path, command, *,
                       web_registry=None):
    from coding_agent import teams as teams_mod
    from coding_agent.providers.base import ToolUseBlock
    from coding_agent.runtime.events import event_context
    from coding_agent.runtime.execution import execution_context
    from coding_agent.web.approvals import web_permission_context

    calls = {"bash": 0}
    provider = _SequenceProvider(SimpleNamespace(
        content=[ToolUseBlock(
            id="toolu_team",
            name="bash",
            input={"command": command},
        )],
        stop_reason="tool_use",
    ))
    fake_bus = _FakeBus()
    teammate_name = f"worker_{uuid4().hex[:8]}"
    monkeypatch.setattr(teams_mod, "BUS", fake_bus)
    monkeypatch.setattr(teams_mod, "get_model_provider", lambda: provider)
    monkeypatch.setattr(teams_mod, "run_bash",
                        lambda command, cwd=None: (
                            calls.__setitem__("bash", calls["bash"] + 1)
                            or "ran"
                        ))
    monkeypatch.setattr(teams_mod, "IDLE_TIMEOUT", 0)
    monkeypatch.setattr(teams_mod, "IDLE_POLL_INTERVAL", 1)

    managers = [
        event_context(
            session_id="session_team",
            run_id="run_team",
            source="web",
            workspace=tmp_path,
        )
    ]
    if web_registry is not None:
        managers.append(web_permission_context(web_registry))

    with execution_context(
        session_id="session_team",
        run_id="run_team",
        source="web",
        workspace=tmp_path,
    ), managers[0]:
        if len(managers) == 2:
            with managers[1]:
                result = teams_mod.spawn_teammate_thread(
                    teammate_name, "tester", "run one tool")
        else:
            result = teams_mod.spawn_teammate_thread(
                teammate_name, "tester", "run one tool")

    deadline = time.time() + 3
    while time.time() < deadline and teammate_name in teams_mod.active_teammates:
        time.sleep(0.01)
    teams_mod.active_teammates.pop(teammate_name, None)
    return result, provider.tool_result, calls["bash"], fake_bus.sent


def test_teammate_denied_tool_does_not_execute(monkeypatch, tmp_path):
    result, tool_result, bash_calls, sent = _run_teammate_bash(
        monkeypatch, tmp_path, "sudo reboot")

    assert result.startswith("Teammate 'worker_")
    assert bash_calls == 0
    assert "deny list" in tool_result
    assert any(message["type"] == "result" for message in sent)


def test_teammate_benign_bash_is_not_exposed_or_executed(monkeypatch, tmp_path):
    result, tool_result, bash_calls, sent = _run_teammate_bash(
        monkeypatch,
        tmp_path,
        "python -c \"print('escape')\"",
    )

    assert result.startswith("Teammate 'worker_")
    assert bash_calls == 0
    assert tool_result == (
        "Permission denied: detached Web teammate cannot await approval."
    )
    assert any(message["type"] == "result" for message in sent)


def test_teammate_web_ask_uses_web_resolver_without_input(monkeypatch, tmp_path):
    from coding_agent.web.approvals import ApprovalRegistry

    def fail_input(*args, **kwargs):
        raise AssertionError("web teammate resolver must not call input")

    monkeypatch.setattr("builtins.input", fail_input)
    registry = ApprovalRegistry(timeout_seconds=0.01, workspace=tmp_path)

    result, tool_result, bash_calls, _sent = _run_teammate_bash(
        monkeypatch,
        tmp_path,
        "rm build/output.txt",
        web_registry=registry,
    )

    assert result.startswith("Teammate 'worker_")
    assert bash_calls == 0
    assert tool_result == (
        "Permission denied: detached Web teammate cannot await approval."
    )
    assert registry.list(include_resolved=True) == []
    events = _events(tmp_path)
    teammate_permission_events = [
        event
        for event in events
        if event["type"] == "permission_decision"
    ]
    assert any(
        event["type"] == "permission_decision"
        and event.get("source") == "web_child_auto_deny"
        and event["payload"].get("source") == "web_child_auto_deny"
        for event in events
    )
    assert teammate_permission_events
    assert all(
        event.get("session_id") == "session_team"
        and event.get("run_id") is None
        for event in teammate_permission_events
    )


def test_execution_context_routes_workspace_local_state(monkeypatch, tmp_path):
    from coding_agent import cron_scheduler as cron_mod
    from coding_agent import teams as teams_mod
    from coding_agent.mcp import client as mcp_client_mod
    from coding_agent.memory.context import assemble_system_prompt, update_context
    from coding_agent.memory.store import append_memory, read_memory_for_tool
    from coding_agent.runtime.execution import execution_context
    from coding_agent.task_system import tasks as tasks_mod
    from coding_agent.task_system import worktrees as worktrees_mod

    process_workspace = tmp_path / "process"
    web_workspace = tmp_path / "web"
    process_workspace.mkdir()
    web_workspace.mkdir()
    monkeypatch.chdir(process_workspace)
    (process_workspace / ".memory").mkdir()
    (process_workspace / ".memory" / "MEMORY.md").write_text(
        "process memory", encoding="utf-8")
    (web_workspace / ".memory").mkdir()
    (web_workspace / ".memory" / "MEMORY.md").write_text(
        "web memory", encoding="utf-8")
    (web_workspace / ".mcp.json").write_text(json.dumps({
        "servers": {
            "local": {
                "command": "fake-mcp",
                "args": [],
            }
        }
    }), encoding="utf-8")

    git_cwds = []

    def fake_git(args, cwd, capture_output, text, timeout):
        git_cwds.append(Path(cwd).resolve())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    discovered = [{
        "name": "echo",
        "description": "Echo",
        "inputSchema": {"type": "object", "properties": {}},
    }]
    transport_configs = []

    class FakeTransport:
        def __init__(self, config):
            transport_configs.append(config)

        def initialize(self):
            return discovered, None

        def close(self):
            return None

        def call_tool(self, tool_name, args):
            return "ok"

    monkeypatch.setattr(worktrees_mod.subprocess, "run", fake_git)
    monkeypatch.setattr(mcp_client_mod, "StdioMCPTransport", FakeTransport)
    clients = mcp_client_mod.current_mcp_clients(web_workspace)
    clients.clear()

    bus = teams_mod.MessageBus()
    with execution_context(
        workspace=web_workspace,
        session_id="session_workspace",
        run_id="run_workspace",
        source="web",
    ):
        assert read_memory_for_tool() == "web memory"
        assert append_memory("web appended").startswith("Appended memory")
        context = update_context({}, [])
        system = assemble_system_prompt(context)
        task = tasks_mod.create_task("web task")
        bus.send("lead", "worker", "web message")
        worktrees_mod.run_git(["status", "--short"])
        worktrees_mod.worktrees_dir()
        cron_result = cron_mod.run_schedule_cron(
            "59 23 31 12 6", "web cron", durable=True)
        mcp_result = mcp_client_mod.connect_mcp("local")
        mcp_tools, _ = mcp_client_mod.mcp_tool_entries()

    assert "web memory" in context["memories"]
    assert str(web_workspace.resolve()) in system
    assert (web_workspace / ".tasks" / f"{task.id}.json").exists()
    assert (web_workspace / ".mailboxes" / "worker.jsonl").exists()
    assert (web_workspace / ".worktrees").is_dir()
    assert git_cwds == [web_workspace.resolve()]
    assert "Scheduled" in cron_result
    assert (web_workspace / ".scheduled_tasks.json").exists()
    assert "Connected to MCP server 'local'" in mcp_result
    assert transport_configs[0].workspace == str(web_workspace.resolve())
    assert [tool["name"] for tool in mcp_tools] == ["mcp__local__echo"]

    process_memory = (
        process_workspace / ".memory" / "MEMORY.md"
    ).read_text(encoding="utf-8")
    assert process_memory == "process memory"
    assert not (process_workspace / ".tasks").exists()
    assert not (process_workspace / ".mailboxes").exists()
    assert not (process_workspace / ".worktrees").exists()
    assert not (process_workspace / ".scheduled_tasks.json").exists()


def test_legacy_workspace_path_constants_remain_compatible(
        monkeypatch, tmp_path):
    from coding_agent import cron_scheduler as cron_mod
    from coding_agent import teams as teams_mod
    from coding_agent.config import WORKDIR
    from coding_agent.memory import context as memory_context
    from coding_agent.task_system import tasks as tasks_mod
    from coding_agent.task_system import worktrees as worktrees_mod

    assert tasks_mod.TASKS_DIR == WORKDIR / ".tasks"
    assert worktrees_mod.WORKTREES_DIR == WORKDIR / ".worktrees"
    assert teams_mod.TASKS_DIR == WORKDIR / ".tasks"
    assert teams_mod.WORKTREES_DIR == WORKDIR / ".worktrees"
    assert teams_mod.MAILBOX_DIR == WORKDIR / ".mailboxes"
    assert memory_context.MEMORY_DIR == WORKDIR / ".memory"
    assert memory_context.MEMORY_INDEX == WORKDIR / ".memory" / "MEMORY.md"

    custom_durable_path = tmp_path / "legacy-scheduled-tasks.json"
    monkeypatch.setattr(cron_mod, "DURABLE_PATH", custom_durable_path)
    assert cron_mod.durable_path() == custom_durable_path.resolve()
    assert cron_mod.durable_path(WORKDIR) == custom_durable_path.resolve()


def test_background_subagent_and_teammate_inherit_web_workspace(
        monkeypatch, tmp_path):
    from coding_agent import background as background_mod
    from coding_agent import teams as teams_mod
    from coding_agent.providers.base import TextBlock, ToolUseBlock
    from coding_agent.runtime.events import event_context
    from coding_agent.runtime.execution import execution_context
    from coding_agent.tools import subagent as subagent_mod
    from coding_agent.tools.basic import run_write

    workspace = tmp_path / "web"
    workspace.mkdir()
    background_mod.background_tasks.clear()
    background_mod.background_results.clear()
    block = SimpleNamespace(
        id="toolu_bg_workspace",
        name="bash",
        input={"command": "background"},
    )
    with execution_context(
        workspace=workspace,
        session_id="session_children",
        run_id="run_children",
        source="web",
    ), event_context(
        workspace=workspace,
        session_id="session_children",
        run_id="run_children",
        source="web",
    ):
        bg_id = background_mod.start_background_task(
            block,
            {"bash": lambda command: run_write("background.txt", command)},
        )
    deadline = time.time() + 2
    while (time.time() < deadline
           and background_mod.background_tasks[bg_id]["status"] != "completed"):
        time.sleep(0.01)
    assert (workspace / "background.txt").read_text(
        encoding="utf-8") == "background"

    sub_provider = _SequenceProvider(SimpleNamespace(
        content=[ToolUseBlock(
            id="toolu_sub_workspace",
            name="write_file",
            input={"path": "subagent.txt", "content": "subagent"},
        )],
        stop_reason="tool_use",
    ))
    monkeypatch.setattr(
        subagent_mod, "get_model_provider", lambda: sub_provider)
    with execution_context(
        workspace=workspace,
        session_id="session_children",
        run_id="run_children",
        source="web",
    ), event_context(
        workspace=workspace,
        session_id="session_children",
        run_id="run_children",
        source="web",
    ):
        subagent_mod.spawn_subagent("write from subagent")
    assert (workspace / "subagent.txt").read_text(
        encoding="utf-8") == "subagent"

    teammate_provider = _SequenceProvider(SimpleNamespace(
        content=[ToolUseBlock(
            id="toolu_team_workspace",
            name="write_file",
            input={"path": "teammate.txt", "content": "teammate"},
        )],
        stop_reason="tool_use",
    ))
    teammate_name = f"workspace_{uuid4().hex[:8]}"
    monkeypatch.setattr(teams_mod, "BUS", _FakeBus())
    monkeypatch.setattr(
        teams_mod, "get_model_provider", lambda: teammate_provider)
    monkeypatch.setattr(teams_mod, "IDLE_TIMEOUT", 0)
    with execution_context(
        workspace=workspace,
        session_id="session_children",
        run_id="run_children",
        source="web",
    ), event_context(
        workspace=workspace,
        session_id="session_children",
        run_id="run_children",
        source="web",
    ):
        teams_mod.spawn_teammate_thread(
            teammate_name, "tester", "write from teammate")
    deadline = time.time() + 3
    while time.time() < deadline and teammate_name in teams_mod.active_teammates:
        time.sleep(0.01)
    teams_mod.active_teammates.pop(teammate_name, None)
    assert (workspace / "teammate.txt").read_text(
        encoding="utf-8") == "teammate"


@pytest.mark.parametrize(
    ("decision", "expected_calls"),
    [("allow", 1), ("deny", 0)],
)
def test_synchronous_subagent_web_approval_allow_and_deny(
        monkeypatch, tmp_path, decision, expected_calls):
    import threading

    from coding_agent.providers.base import ToolUseBlock
    from coding_agent.runtime.events import event_context
    from coding_agent.runtime.execution import execution_context
    from coding_agent.tools import subagent as subagent_mod
    from coding_agent.web.approvals import ApprovalRegistry, web_permission_context

    provider = _SequenceProvider(SimpleNamespace(
        content=[ToolUseBlock(
            id=f"toolu_sub_{decision}",
            name="bash",
            input={"command": "rm build/output.txt"},
        )],
        stop_reason="tool_use",
    ))
    calls = {"bash": 0}
    registry = ApprovalRegistry(timeout_seconds=2, workspace=tmp_path)
    monkeypatch.setattr(subagent_mod, "get_model_provider", lambda: provider)
    monkeypatch.setitem(
        subagent_mod.SUB_HANDLERS,
        "bash",
        lambda command: calls.__setitem__("bash", calls["bash"] + 1) or "ran",
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Web subagent must not call input")),
    )

    def resolve():
        deadline = time.time() + 1
        while time.time() < deadline:
            approvals = registry.list(include_resolved=False)
            if approvals:
                registry.resolve(approvals[0]["approval_id"], decision)
                return
            time.sleep(0.01)
        raise AssertionError("approval was not created")

    resolver = threading.Thread(target=resolve)
    resolver.start()
    with execution_context(
        workspace=tmp_path,
        session_id="session_sub_approval",
        run_id=f"run_sub_{decision}",
        source="web",
    ), event_context(
        workspace=tmp_path,
        session_id="session_sub_approval",
        run_id=f"run_sub_{decision}",
        source="web",
    ), web_permission_context(registry):
        subagent_mod.spawn_subagent("approval test")
    resolver.join(timeout=2)

    assert not resolver.is_alive()
    assert calls["bash"] == expected_calls
    approval = registry.list(include_resolved=True)[0]
    assert approval["status"] == (
        "allowed" if decision == "allow" else "denied")


def test_cli_permission_resolver_still_uses_input(monkeypatch):
    from coding_agent.hooks import cli_permission_resolver
    from coding_agent.security.policy import PolicyDecision

    calls = {"input": 0}

    def allow(_prompt):
        calls["input"] += 1
        return "y"

    monkeypatch.setattr("builtins.input", allow)
    result = cli_permission_resolver(PolicyDecision(
        action="ask",
        tool="bash",
        reason="test ask",
        subject="rm output.txt",
    ))

    assert result is None
    assert calls["input"] == 1


def test_teammate_submit_plan_triggers_post_tool_hook(monkeypatch):
    from coding_agent import teams as teams_mod
    from coding_agent.providers.base import TextBlock, ToolUseBlock

    teammate_name = f"planner_{uuid4().hex[:8]}"
    hook_calls = []

    class PlanBus(_FakeBus):
        def __init__(self):
            super().__init__()
            self.responded = False

        def read_inbox(self, agent):
            if agent != teammate_name or self.responded:
                return []
            request = next(
                (message for message in self.sent
                 if message["type"] == "plan_approval_request"),
                None,
            )
            if request is None:
                return []
            self.responded = True
            return [{
                "from": "lead",
                "to": teammate_name,
                "content": "approved",
                "type": "plan_approval_response",
                "metadata": {
                    "request_id": request["metadata"]["request_id"],
                    "approve": True,
                },
            }]

    provider = _SequenceProvider(SimpleNamespace(
        content=[ToolUseBlock(
            id="toolu_submit_plan",
            name="submit_plan",
            input={"plan": "implement safely"},
        )],
        stop_reason="tool_use",
    ))
    monkeypatch.setattr(teams_mod, "BUS", PlanBus())
    monkeypatch.setattr(teams_mod, "get_model_provider", lambda: provider)
    monkeypatch.setattr(teams_mod, "IDLE_TIMEOUT", 0)

    def capture_hook(event, block, *args):
        hook_calls.append((event, block.name))
        return None

    monkeypatch.setattr(teams_mod, "trigger_hooks", capture_hook)
    teams_mod.spawn_teammate_thread(
        teammate_name, "planner", "submit a plan")
    deadline = time.time() + 3
    while time.time() < deadline and teammate_name in teams_mod.active_teammates:
        time.sleep(0.01)
    teams_mod.active_teammates.pop(teammate_name, None)

    assert ("PreToolUse", "submit_plan") in hook_calls
    assert ("PostToolUse", "submit_plan") in hook_calls


def test_web_package_does_not_call_run_cli():
    web_root = Path(__file__).parents[1] / "coding_agent" / "web"
    offenders = [
        path
        for path in web_root.rglob("*.py")
        if "run_cli(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
