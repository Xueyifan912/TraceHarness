import json
import os
import queue
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from fastapi.testclient import TestClient


def _read_events(workspace):
    path = workspace / ".agent_events" / "events.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _wait_for_pending(client, *, expected_count=1, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(
            "/api/approvals",
            params={"include_resolved": False},
        )
        assert response.status_code == 200
        approvals = response.json()["approvals"]
        if len(approvals) >= expected_count:
            return approvals
        time.sleep(0.01)
    raise AssertionError("approval was not created")


def _web_client(tmp_path, monkeypatch, *, handler_output="tool executed"):
    from coding_agent.providers.base import ModelResponse, TextBlock, ToolUseBlock
    from coding_agent.runtime import loop as loop_mod
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    calls = {"handler": 0, "llm": 0}

    def fake_call_llm(messages, context, tools, state, max_tokens):
        calls["llm"] += 1
        has_result = any(
            isinstance(message.get("content"), list)
            and any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in message["content"]
            )
            for message in messages
            if isinstance(message, dict)
        )
        if not has_result:
            return ModelResponse(
                content=[
                    ToolUseBlock(
                        id=f"toolu_rm_{len(messages)}",
                        name="bash",
                        input={"command": "rm build/output.txt"},
                    )
                ],
                stop_reason="tool_use",
                id=f"msg_tool_{calls['llm']}",
            )
        return ModelResponse(
            content=[TextBlock("final response")],
            stop_reason="end_turn",
            id=f"msg_final_{calls['llm']}",
        )

    def fake_handler(command):
        calls["handler"] += 1
        return handler_output

    monkeypatch.setattr(loop_mod, "call_llm", fake_call_llm)
    monkeypatch.setattr(
        loop_mod,
        "assemble_tool_pool",
        lambda: ([{"name": "bash"}], {"bash": fake_handler}),
    )

    service = AgentService(workspace=tmp_path)
    return TestClient(create_app(service=service)), calls


def _post_message_in_thread(client, session_id, content="needs approval"):
    responses = queue.Queue()

    def run_request():
        responses.put(client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": content},
        ))

    thread = threading.Thread(target=run_request)
    thread.start()
    return thread, responses


def test_web_policy_ask_creates_pending_and_allow_continues_tool(
        monkeypatch, tmp_path):
    client, calls = _web_client(tmp_path, monkeypatch)
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]
    thread, responses = _post_message_in_thread(client, session_id)

    approvals = _wait_for_pending(client)
    approval = approvals[0]
    assert approval["session_id"] == session_id
    assert approval["run_id"].startswith("run_")
    assert approval["tool_name"] == "bash"
    assert approval["tool_use_id"] == "toolu_rm_1"
    assert approval["status"] == "pending"

    detail = client.get(f"/api/approvals/{approval['approval_id']}")
    assert detail.status_code == 200
    assert detail.json()["approval"]["approval_id"] == approval["approval_id"]

    session_detail = client.get(f"/api/sessions/{session_id}").json()
    assert session_detail["session"]["status"] == "waiting_approval"
    assert session_detail["session"]["active_run_id"] == approval["run_id"]

    resolved = client.post(
        f"/api/approvals/{approval['approval_id']}",
        json={"decision": "allow", "message": "approved by test"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["approval"]["status"] == "allowed"
    assert resolved.json()["approval"]["decision"] == "allow"

    thread.join(timeout=5)
    assert not thread.is_alive()
    response = responses.get_nowait()
    assert response.status_code == 200
    assert response.json()["run"]["status"] == "completed"
    assert response.json()["session"]["status"] == "idle"
    assert calls["handler"] == 1

    events = _read_events(tmp_path)
    event_types = [event["type"] for event in events]
    assert "approval_requested" in event_types
    assert "approval_resolved" in event_types
    permission_events = [
        event["payload"] for event in events
        if event["type"] == "permission_decision"
    ]
    assert [event["action"] for event in permission_events] == ["ask", "allow"]
    assert permission_events[-1]["source"] == "web_approval"


def test_web_policy_deny_returns_tool_result_without_crash(monkeypatch, tmp_path):
    client, calls = _web_client(tmp_path, monkeypatch)
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]
    thread, responses = _post_message_in_thread(client, session_id)

    approval = _wait_for_pending(client)[0]
    denied = client.post(
        f"/api/approvals/{approval['approval_id']}",
        json={"decision": "deny", "message": "not allowed"},
    )
    assert denied.status_code == 200
    assert denied.json()["approval"]["status"] == "denied"

    thread.join(timeout=5)
    response = responses.get_nowait()
    assert response.status_code == 200
    assert calls["handler"] == 0

    messages = client.get(f"/api/sessions/{session_id}").json()["messages"]
    tool_results = [
        block
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert tool_results
    assert tool_results[-1]["content"] == "not allowed"

    permission_events = [
        event["payload"] for event in _read_events(tmp_path)
        if event["type"] == "permission_decision"
    ]
    assert [event["action"] for event in permission_events] == ["ask", "deny"]
    assert permission_events[-1]["source"] == "web_approval"


def test_approval_api_errors(monkeypatch, tmp_path):
    client, _calls = _web_client(tmp_path, monkeypatch)
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]
    thread, responses = _post_message_in_thread(client, session_id)
    approval = _wait_for_pending(client)[0]

    missing = client.get("/api/approvals/appr_missing")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "approval_not_found"

    invalid = client.post(
        f"/api/approvals/{approval['approval_id']}",
        json={"decision": "maybe"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"

    too_long = client.post(
        f"/api/approvals/{approval['approval_id']}",
        json={"decision": "deny", "message": "x" * 2001},
    )
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "validation_error"

    mismatch = client.post(
        f"/api/approvals/{approval['approval_id']}",
        params={"session_id": "different-session"},
        json={"decision": "allow"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "approval_mismatch"

    ok = client.post(
        f"/api/approvals/{approval['approval_id']}",
        json={"decision": "deny"},
    )
    assert ok.status_code == 200

    again = client.post(
        f"/api/approvals/{approval['approval_id']}",
        json={"decision": "allow"},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "approval_already_resolved"

    thread.join(timeout=5)
    assert responses.get_nowait().status_code == 200


def test_pending_approvals_do_not_cross_sessions(monkeypatch, tmp_path):
    client, _calls = _web_client(tmp_path, monkeypatch)
    session_a = client.post("/api/sessions", json={}).json()["session"]["session_id"]
    session_b = client.post("/api/sessions", json={}).json()["session"]["session_id"]
    thread_a, responses_a = _post_message_in_thread(client, session_a, "a")
    thread_b, responses_b = _post_message_in_thread(client, session_b, "b")

    approvals = _wait_for_pending(client, expected_count=2)
    by_session = {approval["session_id"]: approval for approval in approvals}
    assert set(by_session) == {session_a, session_b}
    assert by_session[session_a]["run_id"] != by_session[session_b]["run_id"]

    only_a = client.get(
        "/api/approvals",
        params={"session_id": session_a, "include_resolved": False},
    ).json()["approvals"]
    assert len(only_a) == 1
    assert only_a[0]["session_id"] == session_a

    for approval in approvals:
        response = client.post(
            f"/api/approvals/{approval['approval_id']}",
            json={"decision": "deny", "message": f"deny {approval['session_id']}"},
        )
        assert response.status_code == 200

    thread_a.join(timeout=5)
    thread_b.join(timeout=5)
    assert responses_a.get_nowait().status_code == 200
    assert responses_b.get_nowait().status_code == 200


def test_approval_timeout_defaults_to_deny(monkeypatch, tmp_path):
    from coding_agent.web.approvals import (
        ApprovalRegistry,
        WEB_APPROVAL_EXPIRED_REASON,
    )
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    client, calls = _web_client(tmp_path, monkeypatch)
    service = AgentService(
        workspace=tmp_path,
        approval_registry=ApprovalRegistry(timeout_seconds=0.01),
    )
    # Reuse the monkeypatched loop/tool behavior, but with a short-timeout service.
    client = TestClient(create_app(service=service))
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]

    response = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": "will timeout"},
    )
    assert response.status_code == 200
    assert calls["handler"] == 0

    approvals = client.get("/api/approvals").json()["approvals"]
    assert approvals[0]["status"] == "expired"
    assert approvals[0]["message"] == WEB_APPROVAL_EXPIRED_REASON

    permission_events = [
        event["payload"] for event in _read_events(tmp_path)
        if event["type"] == "permission_decision"
    ]
    assert permission_events[-1]["source"] == "web_approval_timeout"


def test_approval_cancel_defaults_to_deny(tmp_path):
    from coding_agent.runtime.events import event_context
    from coding_agent.security.policy import PolicyDecision
    from coding_agent.web.approvals import (
        ApprovalRegistry,
        WEB_APPROVAL_CANCELLED_REASON,
    )

    registry = ApprovalRegistry(timeout_seconds=30)
    with event_context(
        session_id="session_cancel",
        run_id="run_cancel",
        source="web",
        workspace=tmp_path,
    ):
        approval = registry.create(
            PolicyDecision(
                action="ask",
                tool="bash",
                reason="test ask",
                subject="rm file",
                tool_use_id="toolu_cancel",
            ),
            session_id="session_cancel",
            run_id="run_cancel",
        )
        cancelled = registry.cancel_run("run_cancel")

    assert cancelled[0]["approval_id"] == approval.approval_id
    assert registry.get(approval.approval_id)["status"] == "cancelled"
    assert registry.get(approval.approval_id)["decision"] == "deny"
    assert registry.get(approval.approval_id)["message"] == WEB_APPROVAL_CANCELLED_REASON


def test_cli_permission_resolver_still_uses_input(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent import hooks

    prompts = []
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt) or "n",
    )
    block = SimpleNamespace(
        name="bash",
        id="toolu_cli",
        input={"command": "rm build/output.txt"},
    )

    result = hooks.permission_hook(block)

    assert prompts == ["  Allow? [y/N] "]
    assert result == "Permission denied by user"
