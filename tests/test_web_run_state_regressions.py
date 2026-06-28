import json
import os
import queue
import threading
import time

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from fastapi.testclient import TestClient


def _wait_for_run(client, session_id, run_id, expected, timeout=5):
    expected_statuses = {expected} if isinstance(expected, str) else set(expected)
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = client.get(
            f"/api/sessions/{session_id}/runs/{run_id}"
        )
        assert response.status_code == 200
        last = response.json()["run"]
        if last["status"] in expected_statuses:
            return last
        time.sleep(0.01)
    raise AssertionError(
        f"run did not reach {expected_statuses}; last={last}"
    )


def _events(client, session_id, run_id):
    response = client.get(
        "/api/events",
        params={
            "session_id": session_id,
            "run_id": run_id,
            "limit": 200,
        },
    )
    assert response.status_code == 200
    return response.json()["events"]


def test_abandon_background_tasks_defaults_to_nonblocking(tmp_path):
    from coding_agent import background as background_mod
    from coding_agent.runtime.events import event_context
    from coding_agent.runtime.execution import execution_context

    workspace = str(tmp_path.resolve())
    context = {
        "session_id": "session_nonblocking",
        "run_id": "run_nonblocking",
        "workspace": workspace,
    }

    with background_mod.background_condition:
        background_mod.background_tasks.clear()
        background_mod.background_results.clear()
        background_mod.background_tasks["bg_nonblocking"] = {
            "tool_use_id": "toolu_nonblocking",
            "command": "blocked background tool",
            "status": "running",
            "context": context,
            "session_id": context["session_id"],
            "run_id": context["run_id"],
            "workspace": workspace,
            "abandoned": False,
        }

    try:
        with execution_context(**context), event_context(**context):
            started = time.monotonic()
            background_mod.abandon_background_tasks()
            elapsed = time.monotonic() - started

        assert elapsed < 0.2
        assert background_mod.background_tasks["bg_nonblocking"]["abandoned"]
    finally:
        with background_mod.background_condition:
            background_mod.background_tasks.clear()
            background_mod.background_results.clear()


def _actual_loop_client(
    monkeypatch,
    tmp_path,
    *,
    handler,
    before_final=None,
):
    from coding_agent.providers.base import (
        ModelResponse,
        TextBlock,
        ToolUseBlock,
    )
    from coding_agent.runtime import loop as loop_mod
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    calls = {"llm": 0}

    def fake_call_llm(messages, context, tools, state, max_tokens):
        del messages, context, tools, state, max_tokens
        calls["llm"] += 1
        if calls["llm"] == 1:
            return ModelResponse(
                content=[
                    ToolUseBlock(
                        id="toolu_regression",
                        name="read_file",
                        input={"path": "fixture.txt"},
                    )
                ],
                stop_reason="tool_use",
                id="msg_tool",
            )
        if before_final is not None:
            before_final()
        return ModelResponse(
            content=[TextBlock("final answer after tool")],
            stop_reason="end_turn",
            id="msg_final",
        )

    monkeypatch.setattr(loop_mod, "call_llm", fake_call_llm)
    monkeypatch.setattr(
        loop_mod,
        "assemble_tool_pool",
        lambda: (
            [{"name": "read_file"}],
            {"read_file": handler},
        ),
    )
    service = AgentService(workspace=tmp_path)
    return TestClient(create_app(service=service)), service


def test_tool_end_does_not_finish_run_before_final_output(
    monkeypatch,
    tmp_path,
):
    tool_started = threading.Event()
    release_tool = threading.Event()
    final_call_started = threading.Event()
    release_final = threading.Event()

    def handler(path):
        assert path == "fixture.txt"
        tool_started.set()
        assert release_tool.wait(timeout=5)
        return "tool output"

    def before_final():
        final_call_started.set()
        assert release_final.wait(timeout=5)

    client, _service = _actual_loop_client(
        monkeypatch,
        tmp_path,
        handler=handler,
        before_final=before_final,
    )
    session_id = client.post(
        "/api/sessions", json={}
    ).json()["session"]["session_id"]
    started = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "use a tool"},
    )
    assert started.status_code == 200
    run_id = started.json()["run"]["run_id"]

    assert tool_started.wait(timeout=5)
    running = client.get(
        f"/api/sessions/{session_id}/runs/{run_id}"
    ).json()["run"]
    assert running["status"] == "running"
    assert running["ended_at"] is None

    release_tool.set()
    assert final_call_started.wait(timeout=5)
    after_tool = client.get(
        f"/api/sessions/{session_id}/runs/{run_id}"
    ).json()["run"]
    session = client.get(
        f"/api/sessions/{session_id}"
    ).json()["session"]
    assert after_tool["status"] == "running"
    assert after_tool["ended_at"] is None
    assert session["status"] == "running"
    assert session["active_run_id"] == run_id

    release_final.set()
    _wait_for_run(client, session_id, run_id, "completed")
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["display_messages"][-1]["content"][0]["text"] == (
        "final answer after tool"
    )

    event_types = [
        event["type"] for event in _events(client, session_id, run_id)
    ]
    assert event_types.index("tool_call_ended") < event_types.index(
        "final_stop"
    )
    assert event_types.index("final_stop") < event_types.index(
        "assistant_message"
    )
    assert event_types.index("assistant_message") < event_types.index(
        "run_completed"
    )


def test_tool_failure_has_explicit_failed_terminal_state(
    monkeypatch,
    tmp_path,
):
    def handler(path):
        del path
        raise RuntimeError("tool exploded")

    client, _service = _actual_loop_client(
        monkeypatch,
        tmp_path,
        handler=handler,
    )
    session_id = client.post(
        "/api/sessions", json={}
    ).json()["session"]["session_id"]
    run = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "fail the tool"},
    ).json()["run"]

    failed = _wait_for_run(
        client, session_id, run["run_id"], "failed"
    )
    assert failed["error"] == "RuntimeError: tool exploded"
    assert failed["ended_at"] is not None
    event_types = [
        event["type"]
        for event in _events(client, session_id, run["run_id"])
    ]
    assert event_types.index("tool_call_ended") < event_types.index(
        "final_stop"
    )
    assert event_types[-1] == "run_failed"


def test_cancel_during_tool_keeps_run_active_until_worker_exits(
    monkeypatch,
    tmp_path,
):
    tool_started = threading.Event()
    release_tool = threading.Event()

    def handler(path):
        del path
        tool_started.set()
        assert release_tool.wait(timeout=5)
        return "tool stopped"

    client, _service = _actual_loop_client(
        monkeypatch,
        tmp_path,
        handler=handler,
    )
    session_id = client.post(
        "/api/sessions", json={}
    ).json()["session"]["session_id"]
    run = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "cancel during tool"},
    ).json()["run"]
    run_id = run["run_id"]
    assert tool_started.wait(timeout=5)

    cancel_response = client.post(
        f"/api/sessions/{session_id}/runs/{run_id}/cancel"
    )
    assert cancel_response.status_code == 200
    assert cancel_response.json()["run"]["status"] == "cancelling"
    assert client.get(
        f"/api/sessions/{session_id}"
    ).json()["session"]["active_run_id"] == run_id
    blocked = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "must wait for cancellation"},
    )
    assert blocked.status_code == 409

    release_tool.set()
    cancelled = _wait_for_run(
        client, session_id, run_id, "cancelled"
    )
    assert cancelled["error"] == "Run cancelled by user."
    event_types = [
        event["type"] for event in _events(client, session_id, run_id)
    ]
    assert "run_cancel_requested" in event_types
    assert event_types[-1] == "run_cancelled"


def test_cancel_active_run_even_when_session_snapshot_is_corrupt(
    monkeypatch,
    tmp_path,
):
    from coding_agent.runtime.session import session_file_path

    tool_started = threading.Event()
    release_tool = threading.Event()

    def handler(path):
        del path
        tool_started.set()
        assert release_tool.wait(timeout=5)
        return "tool stopped"

    client, service = _actual_loop_client(
        monkeypatch,
        tmp_path,
        handler=handler,
    )
    session_id = client.post(
        "/api/sessions", json={}
    ).json()["session"]["session_id"]
    run = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "cancel after snapshot corruption"},
    ).json()["run"]
    run_id = run["run_id"]
    assert tool_started.wait(timeout=5)

    session_file_path(session_id, tmp_path).write_text(
        "{not-json",
        encoding="utf-8",
    )

    cancel_response = client.post(
        f"/api/sessions/{session_id}/runs/{run_id}/cancel"
    )
    assert cancel_response.status_code == 200
    assert cancel_response.json()["run"]["status"] == "cancelling"
    assert service.registry.get_run(run_id).status == "cancelling"

    release_tool.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        cancelled = service.registry.get_run(run_id)
        if cancelled.status == "cancelled":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("run did not reach cancelled")

    assert cancelled.error == "Run cancelled by user."


def test_background_tool_belongs_to_run_until_result_is_consumed(
    monkeypatch,
    tmp_path,
):
    from coding_agent import background as background_mod
    from coding_agent.providers.base import (
        ModelResponse,
        TextBlock,
        ToolUseBlock,
    )
    from coding_agent.runtime import loop as loop_mod
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    background_mod.background_tasks.clear()
    background_mod.background_results.clear()
    (tmp_path / ".agent_policy.yaml").write_text(
        "bash:\n  default_action: allow\n",
        encoding="utf-8",
    )
    tool_started = threading.Event()
    release_tool = threading.Event()
    premature_final_returned = threading.Event()
    calls = {"llm": 0}

    def fake_call_llm(messages, context, tools, state, max_tokens):
        del messages, context, tools, state, max_tokens
        calls["llm"] += 1
        if calls["llm"] == 1:
            return ModelResponse(
                content=[
                    ToolUseBlock(
                        id="toolu_background",
                        name="bash",
                        input={
                            "command": "long-running-command",
                            "run_in_background": True,
                        },
                    )
                ],
                stop_reason="tool_use",
                id="msg_background",
            )
        if calls["llm"] == 2:
            premature_final_returned.set()
            return ModelResponse(
                content=[TextBlock("premature answer")],
                stop_reason="end_turn",
                id="msg_premature",
            )
        return ModelResponse(
            content=[TextBlock("answer after background result")],
            stop_reason="end_turn",
            id="msg_final",
        )

    def background_handler(command, run_in_background=False):
        del run_in_background
        assert command == "long-running-command"
        tool_started.set()
        assert release_tool.wait(timeout=5)
        return "background result"

    monkeypatch.setattr(loop_mod, "call_llm", fake_call_llm)
    monkeypatch.setattr(
        loop_mod,
        "assemble_tool_pool",
        lambda: (
            [{"name": "bash"}],
            {"bash": background_handler},
        ),
    )
    service = AgentService(workspace=tmp_path)
    client = TestClient(create_app(service=service))
    session_id = client.post(
        "/api/sessions", json={}
    ).json()["session"]["session_id"]
    run = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "run a background tool"},
    ).json()["run"]
    run_id = run["run_id"]

    assert tool_started.wait(timeout=5)
    assert premature_final_returned.wait(timeout=5)
    try:
        before_tool_result = client.get(
            f"/api/sessions/{session_id}/runs/{run_id}"
        ).json()["run"]
    finally:
        release_tool.set()
    assert before_tool_result["status"] == "running"
    assert before_tool_result["ended_at"] is None

    _wait_for_run(client, session_id, run_id, "completed")
    assert calls["llm"] == 3
    detail = client.get(f"/api/sessions/{session_id}").json()
    assistant_texts = [
        message["content"][0]["text"]
        for message in detail["display_messages"]
        if message["role"] == "assistant"
    ]
    assert assistant_texts == ["answer after background result"]

    events = _events(client, session_id, run_id)
    types = [event["type"] for event in events]
    assert types.index("background_completion") < types.index("final_stop")
    assert types.index("assistant_message") < types.index("run_completed")


def test_cancelled_background_tool_does_not_leak_late_run_events(
    monkeypatch,
    tmp_path,
):
    from coding_agent import background as background_mod
    from coding_agent.providers.base import (
        ModelResponse,
        TextBlock,
        ToolUseBlock,
    )
    from coding_agent.runtime import loop as loop_mod
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    background_mod.background_tasks.clear()
    background_mod.background_results.clear()
    (tmp_path / ".agent_policy.yaml").write_text(
        "bash:\n  default_action: allow\n",
        encoding="utf-8",
    )
    tool_started = threading.Event()
    release_tool = threading.Event()
    wait_turn_started = threading.Event()
    calls = {"llm": 0}

    def fake_call_llm(messages, context, tools, state, max_tokens):
        del messages, context, tools, state, max_tokens
        calls["llm"] += 1
        if calls["llm"] == 1:
            return ModelResponse(
                content=[
                    ToolUseBlock(
                        id="toolu_cancel_background",
                        name="bash",
                        input={
                            "command": "cancel-background",
                            "run_in_background": True,
                        },
                    )
                ],
                stop_reason="tool_use",
                id="msg_background",
            )
        wait_turn_started.set()
        return ModelResponse(
            content=[TextBlock("wait for background")],
            stop_reason="end_turn",
            id="msg_wait",
        )

    def handler(command, run_in_background=False):
        del command, run_in_background
        tool_started.set()
        assert release_tool.wait(timeout=5)
        return "late result"

    monkeypatch.setattr(loop_mod, "call_llm", fake_call_llm)
    monkeypatch.setattr(
        loop_mod,
        "assemble_tool_pool",
        lambda: ([{"name": "bash"}], {"bash": handler}),
    )
    service = AgentService(workspace=tmp_path)
    client = TestClient(create_app(service=service))
    session_id = client.post(
        "/api/sessions", json={}
    ).json()["session"]["session_id"]
    run = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "cancel background"},
    ).json()["run"]
    run_id = run["run_id"]
    assert tool_started.wait(timeout=5)
    assert wait_turn_started.wait(timeout=5)

    cancel_response = client.post(
        f"/api/sessions/{session_id}/runs/{run_id}/cancel"
    )
    assert cancel_response.json()["run"]["status"] == "cancelling"
    assert client.get(
        f"/api/sessions/{session_id}"
    ).json()["session"]["active_run_id"] == run_id

    release_tool.set()
    cancelled = _wait_for_run(client, session_id, run_id, "cancelled")
    assert cancelled["error"] == "Run cancelled by user."
    assert [
        event["type"] for event in _events(client, session_id, run_id)
    ][-1] == "run_cancelled"

    deadline = time.time() + 5
    while time.time() < deadline and background_mod.background_tasks:
        time.sleep(0.01)
    assert background_mod.background_tasks == {}
    assert background_mod.background_results == {}
    final_types = [
        event["type"] for event in _events(client, session_id, run_id)
    ]
    assert "background_completion" not in final_types
    assert final_types[-1] == "run_cancelled"


def test_approval_resolution_is_published_before_waiter_resumes(
    monkeypatch,
    tmp_path,
):
    from coding_agent.providers.base import (
        ModelResponse,
        TextBlock,
        ToolUseBlock,
    )
    from coding_agent.runtime import loop as loop_mod
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    calls = {"llm": 0}

    def fake_call_llm(messages, context, tools, state, max_tokens):
        del messages, context, tools, state, max_tokens
        calls["llm"] += 1
        if calls["llm"] == 1:
            return ModelResponse(
                content=[
                    ToolUseBlock(
                        id="toolu_approval_order",
                        name="bash",
                        input={"command": "rm output.txt"},
                    )
                ],
                stop_reason="tool_use",
                id="msg_tool",
            )
        return ModelResponse(
            content=[TextBlock("answer after approval")],
            stop_reason="end_turn",
            id="msg_final",
        )

    monkeypatch.setattr(loop_mod, "call_llm", fake_call_llm)
    monkeypatch.setattr(
        loop_mod,
        "assemble_tool_pool",
        lambda: (
            [{"name": "bash"}],
            {"bash": lambda command: f"ran {command}"},
        ),
    )
    service = AgentService(workspace=tmp_path)
    client = TestClient(create_app(service=service))
    session_id = client.post(
        "/api/sessions", json={}
    ).json()["session"]["session_id"]
    run = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "needs approval"},
    ).json()["run"]
    run_id = run["run_id"]

    deadline = time.time() + 5
    approval = None
    while time.time() < deadline:
        pending = client.get(
            "/api/approvals",
            params={
                "session_id": session_id,
                "run_id": run_id,
                "include_resolved": False,
            },
        ).json()["approvals"]
        if pending:
            approval = pending[0]
            break
        time.sleep(0.01)
    assert approval is not None

    log_entered = threading.Event()
    release_log = threading.Event()
    original_log_resolved = service.approval_registry._log_resolved

    def delayed_log_resolved(resolved):
        log_entered.set()
        assert release_log.wait(timeout=5)
        original_log_resolved(resolved)

    monkeypatch.setattr(
        service.approval_registry,
        "_log_resolved",
        delayed_log_resolved,
    )
    responses = queue.Queue()

    def resolve():
        responses.put(client.post(
            f"/api/approvals/{approval['approval_id']}",
            params={"session_id": session_id, "run_id": run_id},
            json={"decision": "allow"},
        ))

    resolver = threading.Thread(target=resolve)
    resolver.start()
    assert log_entered.wait(timeout=5)

    try:
        time.sleep(0.1)
        while_log_is_pending = client.get(
            f"/api/sessions/{session_id}/runs/{run_id}"
        ).json()["run"]
    finally:
        release_log.set()
        resolver.join(timeout=5)
    assert while_log_is_pending["status"] == "waiting_approval"
    assert while_log_is_pending["pending_approval_id"] == (
        approval["approval_id"]
    )

    assert not resolver.is_alive()
    assert responses.get_nowait().status_code == 200
    _wait_for_run(client, session_id, run_id, "completed")

    events = _events(client, session_id, run_id)
    types = [event["type"] for event in events]
    resolved_index = types.index("approval_resolved")
    resumed_index = next(
        index
        for index, event in enumerate(events)
        if index > resolved_index
        and event["type"] == "run_status"
        and event["payload"].get("status") == "running"
    )
    assert resolved_index < resumed_index
    assert resumed_index < types.index("tool_call_ended")
    assert types.index("assistant_message") < types.index("run_completed")
