import os
from types import SimpleNamespace

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from fastapi.testclient import TestClient


class EventLoggingLoop:
    def run(self, messages, context):
        from coding_agent.runtime.events import (
            log_event,
            log_final_stop,
            log_llm_call_ended,
            log_llm_call_started,
            log_tool_call_ended,
            log_tool_call_started,
        )

        block = SimpleNamespace(
            name="bash",
            id="toolu_web",
            input={"command": "echo web"},
        )
        response = SimpleNamespace(
            id="msg_web",
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="done")],
            usage=None,
        )

        log_llm_call_started(
            "test-model",
            message_count=len(messages),
            tool_count=1,
            max_tokens=100,
            timeout_seconds=1,
            provider="fake",
            call_id="call_web",
        )
        log_tool_call_started(block)
        log_event("permission_decision", {
            "action": "allow",
            "tool": "bash",
            "tool_use_id": "toolu_web",
            "reason": "Allowed by test",
            "source": "policy",
        })
        log_tool_call_ended(block, "web output", "completed")
        log_llm_call_ended(
            response, 0.25, "test-model", "fake", "call_web")
        log_final_stop("end_turn", message_count=len(messages) + 1)
        messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": "event response"}],
        })


def test_web_run_events_have_context_and_event_apis(tmp_path):
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    service = AgentService(
        workspace=tmp_path,
        loop_factory=EventLoggingLoop,
    )
    client = TestClient(create_app(service=service))
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]

    turn = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": "write events"},
    )
    assert turn.status_code == 200
    run_id = turn.json()["run"]["run_id"]

    events_response = client.get(
        "/api/events",
        params={"session_id": session_id, "run_id": run_id, "limit": 50},
    )
    assert events_response.status_code == 200
    events = events_response.json()["events"]
    assert events
    assert all(event["session_id"] == session_id for event in events)
    assert all(event["run_id"] == run_id for event in events)
    assert all(event["source"] == "web" for event in events)

    types = [event["type"] for event in events]
    assert "user_prompt_submitted" in types
    assert "llm_call_started" in types
    assert "tool_call_started" in types
    assert "permission_decision" in types
    assert "tool_call_ended" in types
    assert "llm_call_ended" in types
    assert "final_stop" in types

    typed = client.get(
        "/api/events",
        params={"type": "tool_call_started", "limit": 1},
    )
    assert typed.status_code == 200
    assert [event["type"] for event in typed.json()["events"]] == [
        "tool_call_started"
    ]

    session_events = client.get(f"/api/sessions/{session_id}/events")
    assert session_events.status_code == 200
    assert all(
        event["session_id"] == session_id
        for event in session_events.json()["events"]
    )


def test_timeline_merges_tool_events_and_projects_run_activity(tmp_path):
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    service = AgentService(
        workspace=tmp_path,
        loop_factory=EventLoggingLoop,
    )
    client = TestClient(create_app(service=service))
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]
    run_id = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": "timeline"},
    ).json()["run"]["run_id"]

    response = client.get(
        f"/api/sessions/{session_id}/timeline",
        params={"run_id": run_id},
    )
    assert response.status_code == 200
    items = response.json()["items"]

    tool_items = [item for item in items if item["type"] == "tool_call"]
    assert len(tool_items) == 1
    tool = tool_items[0]
    assert tool["title"] == "bash"
    assert tool["status"] == "completed"
    assert tool["tool_use_id"] == "toolu_web"
    assert tool["started_at"]
    assert tool["ended_at"]
    assert tool["input_preview"]["value"]["command"]["preview"] == "echo web"
    assert tool["output_preview"] == "web output"

    llm_items = [item for item in items if item["type"] == "llm_call"]
    assert len(llm_items) == 1
    assert llm_items[0]["id"] == "llm_call_web"
    assert llm_items[0]["status"] == "completed"
    assert llm_items[0]["provider"] == "fake"

    permission_items = [item for item in items if item["type"] == "permission"]
    assert permission_items
    assert permission_items[0]["status"] == "allow"
    assert permission_items[0]["tool_use_id"] == "toolu_web"

    final_items = [item for item in items if item["type"] == "final_stop"]
    assert final_items
    assert final_items[0]["reason"] == "end_turn"

    turn = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": "timeline in response"},
    )
    assert any(
        item["type"] == "tool_call"
        for item in turn.json()["timeline"]
    )


def test_event_store_filters_legacy_events_and_handles_llm_failure(tmp_path):
    from coding_agent.runtime.events import (
        event_context,
        log_event,
        log_llm_call_failed,
        log_llm_call_started,
    )
    from coding_agent.web.event_store import EventStore

    log_event("legacy_without_context", {"value": 1}, workspace=tmp_path)
    with event_context(
        session_id="session_1",
        run_id="run_1",
        source="web",
        workspace=tmp_path,
    ):
        log_llm_call_started(
            "test-model",
            message_count=1,
            tool_count=0,
            max_tokens=100,
            timeout_seconds=1,
            provider="fake",
        )
        log_llm_call_failed(
            RuntimeError("provider failed"),
            elapsed_seconds=0.5,
            model="test-model",
            provider="fake",
        )

    store = EventStore(tmp_path)
    session_events = store.session_events("session_1")["events"]

    assert [event["type"] for event in session_events] == [
        "llm_call_started",
        "llm_call_failed",
    ]
    assert all(event["source"] == "web" for event in session_events)

    timeline = store.timeline(session_id="session_1")["items"]
    assert len(timeline) == 1
    assert timeline[0]["type"] == "llm_call"
    assert timeline[0]["status"] == "failed"
    assert timeline[0]["error_type"] == "RuntimeError"
    assert timeline[0]["message"]["preview"] == "provider failed"


def test_event_store_limit_returns_bounded_recent_events(tmp_path):
    from coding_agent.runtime.events import event_context, log_event
    from coding_agent.web.event_store import EventStore

    with event_context(session_id="session_1", source="web", workspace=tmp_path):
        for index in range(10):
            log_event("numbered", {"index": index})

    events = EventStore(tmp_path).read_events(limit=3)["events"]

    assert [event["payload"]["index"] for event in events] == [7, 8, 9]


def test_event_store_warns_when_cursor_replay_exceeds_limit(tmp_path):
    from coding_agent.runtime.events import event_context, log_event
    from coding_agent.web.event_store import EventStore

    with event_context(session_id="session_1", source="web", workspace=tmp_path):
        for index in range(6):
            log_event("numbered", {"index": index})

    store = EventStore(tmp_path)
    all_events = store.read_events(limit=10)["events"]
    result = store.read_events(
        limit=2,
        cursor=all_events[0]["event_id"],
    )

    assert [event["payload"]["index"] for event in result["events"]] == [4, 5]
    assert result["warnings"] == [
        "More events exist after the requested cursor than "
        "the replay limit allows; a full state resync is required."
    ]


def test_background_completion_finishes_existing_tool_timeline_item():
    from coding_agent.web.event_store import build_timeline

    timeline = build_timeline([
        {
            "event_id": "1",
            "ts": "2026-01-01T00:00:00Z",
            "type": "tool_call_started",
            "payload": {"tool": "bash", "tool_use_id": "toolu_bg"},
        },
        {
            "event_id": "2",
            "ts": "2026-01-01T00:00:01Z",
            "type": "background_completion",
            "payload": {
                "background_id": "bg_0001",
                "tool": "bash",
                "tool_use_id": "toolu_bg",
                "status": "completed",
            },
        },
    ])

    assert len(timeline) == 1
    assert timeline[0]["status"] == "completed"
    assert timeline[0]["background_id"] == "bg_0001"
    assert timeline[0]["ended_at"] == "2026-01-01T00:00:01Z"
