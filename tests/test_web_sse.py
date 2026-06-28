import json
import os
import queue
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from fastapi.testclient import TestClient


class BlockingToolLoop:
    def __init__(self, started=None, release=None, text="background done"):
        self.started = started
        self.release = release
        self.text = text

    def run(self, messages, context):
        from coding_agent.runtime.events import (
            log_final_stop,
            log_tool_call_ended,
            log_tool_call_started,
        )

        block = SimpleNamespace(
            name="bash",
            id="toolu_sse",
            input={"command": "echo sse"},
        )
        log_tool_call_started(block)
        if self.started:
            self.started.set()
        if self.release:
            assert self.release.wait(timeout=5)
        log_tool_call_ended(block, "sse output", "completed")
        log_final_stop("end_turn", message_count=len(messages) + 1)
        messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": self.text}],
        })


class FailingLoop:
    def run(self, messages, context):
        raise RuntimeError("background boom")


class StructuredAssistantLoop:
    def run(self, messages, context):
        from coding_agent.runtime.events import log_final_stop

        messages.append({
            "role": "assistant",
            "content": [{
                "type": "text",
                "text": "你好",
                "citations": None,
            }],
        })
        log_final_stop("end_turn", message_count=len(messages))


def _client(tmp_path, loop_factory):
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    service = AgentService(workspace=tmp_path, loop_factory=loop_factory)
    return TestClient(create_app(service=service))


def _wait_for_status(client, session_id, run_id, expected, timeout=5):
    expected_set = {expected} if isinstance(expected, str) else set(expected)
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = client.get(f"/api/sessions/{session_id}/runs/{run_id}")
        assert response.status_code == 200
        last = response.json()["run"]
        if last["status"] in expected_set:
            return last
        time.sleep(0.01)
    raise AssertionError(f"run did not reach {expected_set}; last={last}")


def _sse_events(text):
    events = []
    for chunk in text.strip().split("\n\n"):
        if not chunk.strip():
            continue
        event_type = None
        event_id = None
        data = None
        for line in chunk.splitlines():
            if line.startswith("id: "):
                event_id = line.removeprefix("id: ")
            if line.startswith("event: "):
                event_type = line.removeprefix("event: ")
            if line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
        if data is not None:
            data["_sse_event"] = event_type
            data["_sse_id"] = event_id
            events.append(data)
    return events


def _post_run_in_thread(client, session_id):
    responses = queue.Queue()

    def request():
        responses.put(client.post(
            f"/api/sessions/{session_id}/runs",
            json={"content": "start background"},
        ))

    thread = threading.Thread(target=request)
    thread.start()
    return thread, responses


def test_background_run_returns_before_completion_and_keeps_lock(tmp_path):
    started = threading.Event()
    release = threading.Event()
    client = _client(
        tmp_path,
        lambda: BlockingToolLoop(started=started, release=release),
    )
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]

    thread, responses = _post_run_in_thread(client, session_id)
    response = responses.get(timeout=1)
    assert response.status_code == 200
    payload = response.json()
    run_id = payload["run"]["run_id"]
    assert payload["run"]["status"] == "running"
    assert payload["session"]["active_run_id"] == run_id
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert started.wait(timeout=5)

    conflict = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "second"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "session_running"

    release.set()
    completed = _wait_for_status(client, session_id, run_id, "completed")
    assert completed["error"] is None


def test_start_run_response_is_consistent_for_fast_run(tmp_path):
    client = _client(tmp_path, StructuredAssistantLoop)
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]

    response = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "finish quickly"},
    )

    assert response.status_code == 200
    payload = response.json()
    run_id = payload["run"]["run_id"]
    assert payload["run"]["status"] == "running"
    assert payload["session"]["status"] == "running"
    assert payload["session"]["active_run_id"] == run_id
    _wait_for_status(client, session_id, run_id, "completed")


def test_sse_stream_replays_ordered_run_events_after_completion(tmp_path):
    started = threading.Event()
    release = threading.Event()
    client = _client(
        tmp_path,
        lambda: BlockingToolLoop(started=started, release=release),
    )
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]
    run = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "stream me"},
    ).json()["run"]
    assert started.wait(timeout=5)
    release.set()
    _wait_for_status(client, session_id, run["run_id"], "completed")

    response = client.get(
        f"/api/sessions/{session_id}/runs/{run['run_id']}/stream"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    replayed = _sse_events(response.text)
    types = [event["type"] for event in replayed]
    ordered = [
        "run_started",
        "tool_call_started",
        "tool_call_ended",
        "run_completed",
    ]
    positions = [types.index(event_type) for event_type in ordered]
    assert positions == sorted(positions)
    persisted = [event for event in replayed if event["type"] != "heartbeat"]
    assert all(event["_sse_id"] == event["event_id"] for event in persisted)

    cursor = persisted[0]["event_id"]
    resumed = client.get(
        f"/api/sessions/{session_id}/runs/{run['run_id']}/stream",
        headers={"Last-Event-ID": cursor},
    )
    resumed_events = _sse_events(resumed.text)
    resumed_ids = [event["event_id"] for event in resumed_events]
    assert cursor not in resumed_ids
    assert resumed_ids == [event["event_id"] for event in persisted[1:]]

    stale = client.get(
        f"/api/sessions/{session_id}/runs/{run['run_id']}/stream",
        headers={"Last-Event-ID": "evt_no_longer_available"},
    )
    stale_events = _sse_events(stale.text)
    assert stale_events[0]["type"] == "stream_gap"
    assert stale_events[0]["_sse_id"] is None
    assert stale_events[0]["payload"]["resync_required"] is True


def test_assistant_message_sse_payload_keeps_structured_content(tmp_path):
    client = _client(tmp_path, StructuredAssistantLoop)
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]
    run = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "structured assistant"},
    ).json()["run"]
    _wait_for_status(client, session_id, run["run_id"], "completed")

    response = client.get(
        f"/api/sessions/{session_id}/runs/{run['run_id']}/stream"
    )
    assert response.status_code == 200
    assert "[{'citations': None" not in response.text
    assert "'citations': None" not in response.text

    events = _sse_events(response.text)
    assistant_events = [
        event for event in events
        if event["type"] == "assistant_message"
    ]
    assert assistant_events
    payload = assistant_events[-1]["payload"]
    assert payload["role"] == "assistant"
    assert payload["content"] == [{
        "type": "text",
        "text": "你好",
        "citations": None,
    }]
    assert payload["text_preview"] == "你好"
    assert payload["truncated"] is False


def test_background_exception_emits_run_failed_and_releases_lock(tmp_path):
    client = _client(tmp_path, FailingLoop)
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]

    first = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "explode"},
    )
    assert first.status_code == 200
    run_id = first.json()["run"]["run_id"]
    failed = _wait_for_status(client, session_id, run_id, "failed")
    assert "RuntimeError: background boom" == failed["error"]

    stream = client.get(f"/api/sessions/{session_id}/runs/{run_id}/stream")
    assert "run_failed" in [event["type"] for event in _sse_events(stream.text)]

    second = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "second attempt"},
    )
    assert second.status_code == 200


def test_subscriber_disconnect_does_not_cancel_background_run(tmp_path):
    from coding_agent.web.event_stream import EVENT_HUB

    started = threading.Event()
    release = threading.Event()
    client = _client(
        tmp_path,
        lambda: BlockingToolLoop(started=started, release=release),
    )
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]
    run = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "observe briefly"},
    ).json()["run"]
    assert started.wait(timeout=5)

    subscriber = EVENT_HUB.subscribe(session_id, run["run_id"])
    EVENT_HUB.unsubscribe(subscriber)

    release.set()
    completed = _wait_for_status(client, session_id, run["run_id"], "completed")
    assert completed["status"] == "completed"


def _approval_client(tmp_path, monkeypatch):
    from coding_agent.providers.base import ModelResponse, TextBlock, ToolUseBlock
    from coding_agent.runtime import loop as loop_mod
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    def fake_call_llm(messages, context, tools, state, max_tokens):
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
                        id="toolu_sse_approval",
                        name="bash",
                        input={"command": "rm build/output.txt"},
                    )
                ],
                stop_reason="tool_use",
                id="msg_tool",
            )
        return ModelResponse(
            content=[TextBlock("handled approval")],
            stop_reason="end_turn",
            id="msg_final",
        )

    monkeypatch.setattr(loop_mod, "call_llm", fake_call_llm)
    monkeypatch.setattr(
        loop_mod,
        "assemble_tool_pool",
        lambda: ([{"name": "bash"}], {"bash": lambda command: "unused"}),
    )

    service = AgentService(workspace=tmp_path)
    return TestClient(create_app(service=service))


def _wait_for_pending_approval(client, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(
            "/api/approvals",
            params={"include_resolved": False},
        )
        assert response.status_code == 200
        approvals = response.json()["approvals"]
        if approvals:
            return approvals[0]
        time.sleep(0.01)
    raise AssertionError("approval was not requested")


def test_approval_events_are_observable_for_background_run(monkeypatch, tmp_path):
    client = _approval_client(tmp_path, monkeypatch)
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]
    started = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "needs approval"},
    )
    assert started.status_code == 200
    run_id = started.json()["run"]["run_id"]

    approval = _wait_for_pending_approval(client)
    assert approval["session_id"] == session_id
    assert approval["run_id"] == run_id
    assert approval["tool_use_id"] == "toolu_sse_approval"

    resolved = client.post(
        f"/api/approvals/{approval['approval_id']}",
        json={"decision": "deny", "message": "blocked in sse test"},
    )
    assert resolved.status_code == 200
    _wait_for_status(client, session_id, run_id, "completed")

    events = client.get(
        "/api/events",
        params={"session_id": session_id, "run_id": run_id, "limit": 100},
    ).json()["events"]
    types = [event["type"] for event in events]
    assert "approval_requested" in types
    assert "approval_resolved" in types
    assert "permission_decision" in types
    assert "run_completed" in types
    waiting_status_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "run_status"
        and event["payload"].get("status") == "waiting_approval"
    )
    assert waiting_status_index < types.index("approval_requested")


def test_approval_survives_sse_disconnect_and_replays_terminal_order(
    monkeypatch,
    tmp_path,
):
    from coding_agent.web.event_stream import EVENT_HUB

    client = _approval_client(tmp_path, monkeypatch)
    session_id = client.post(
        "/api/sessions", json={}
    ).json()["session"]["session_id"]
    run = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "approve after disconnect"},
    ).json()["run"]
    run_id = run["run_id"]
    approval = _wait_for_pending_approval(client)

    subscriber = EVENT_HUB.subscribe(session_id, run_id)
    EVENT_HUB.unsubscribe(subscriber)
    waiting = client.get(
        f"/api/sessions/{session_id}/runs/{run_id}"
    ).json()["run"]
    assert waiting["status"] == "waiting_approval"

    resolved = client.post(
        f"/api/approvals/{approval['approval_id']}",
        params={"session_id": session_id, "run_id": run_id},
        json={"decision": "allow"},
    )
    assert resolved.status_code == 200
    _wait_for_status(client, session_id, run_id, "completed")

    replay = client.get(
        f"/api/sessions/{session_id}/runs/{run_id}/stream"
    )
    replayed = _sse_events(replay.text)
    types = [event["type"] for event in replayed]
    ids = [
        event["event_id"]
        for event in replayed
        if event["type"] not in {"heartbeat", "stream_gap"}
    ]
    assert "approval_requested" in types
    assert "approval_resolved" in types
    assert types[-1] == "run_completed"
    assert len(ids) == len(set(ids))


def test_cancel_waiting_approval_reaches_cancelled_terminal_state(
    monkeypatch,
    tmp_path,
):
    client = _approval_client(tmp_path, monkeypatch)
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]
    started = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "cancel this approval"},
    )
    run_id = started.json()["run"]["run_id"]
    approval = _wait_for_pending_approval(client)

    response = client.post(
        f"/api/sessions/{session_id}/runs/{run_id}/cancel"
    )

    assert response.status_code == 200
    cancelled = _wait_for_status(client, session_id, run_id, "cancelled")
    assert cancelled["error"] == "Run cancelled by user."
    resolved = client.get(
        f"/api/approvals/{approval['approval_id']}"
    ).json()["approval"]
    assert resolved["status"] == "cancelled"
    stream = client.get(
        f"/api/sessions/{session_id}/runs/{run_id}/stream"
    )
    event_types = [event["type"] for event in _sse_events(stream.text)]
    assert "run_cancel_requested" in event_types
    assert event_types[-1] == "run_cancelled"

    next_run = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"content": "new run after cancellation"},
    )
    assert next_run.status_code == 200
    next_run_id = next_run.json()["run"]["run_id"]
    _wait_for_pending_approval(client)
    client.post(f"/api/sessions/{session_id}/runs/{next_run_id}/cancel")
    _wait_for_status(client, session_id, next_run_id, "cancelled")


def test_old_post_messages_still_works_with_shared_registry(tmp_path):
    client = _client(tmp_path, lambda: BlockingToolLoop())
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]

    response = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": "sync message"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run"]["status"] == "completed"
    assert payload["session"]["status"] == "idle"
    assert payload["messages"][0]["content"][0]["text"] == "background done"
