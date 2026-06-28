import json
import os
import threading
import time

import pytest

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


def _events(workspace):
    path = workspace / ".agent_events" / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_agent_service_runs_agent_loop_and_saves_session(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    from coding_agent.providers.base import ModelResponse, TextBlock
    from coding_agent.runtime import loop as loop_mod
    from coding_agent.web.agent_service import AgentService

    calls = []

    def fake_call_llm(messages, context, tools, state, max_tokens):
        calls.append({
            "messages": list(messages),
            "context": dict(context),
            "tool_count": len(tools),
        })
        return ModelResponse(
            content=[TextBlock("service response")],
            stop_reason="end_turn",
            id="msg_service",
        )

    monkeypatch.setattr(loop_mod, "call_llm", fake_call_llm)

    service = AgentService(workspace=tmp_path)
    session_id = service.create_session()["session"]["session_id"]

    response = service.post_message(session_id, "hello from web")

    assert calls, "AgentLoop.run should call the patched LLM function"
    assert response["run"]["status"] == "completed"
    assert response["session"]["status"] == "idle"
    assert response["session"]["message_count"] == 2
    assert response["messages"] == [{
        "role": "assistant",
        "content": [{"text": "service response", "type": "text"}],
    }]

    detail = service.get_session(session_id)
    assert detail["messages"][0] == {"role": "user", "content": "hello from web"}
    assert detail["messages"][1]["role"] == "assistant"


def test_web_permission_ask_times_out_without_input(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    from coding_agent.providers.base import ModelResponse, TextBlock, ToolUseBlock
    from coding_agent.runtime import loop as loop_mod
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.approvals import (
        ApprovalRegistry,
        WEB_APPROVAL_EXPIRED_REASON,
    )

    def fail_input(*args, **kwargs):
        raise AssertionError("Web permission path must not call input()")

    monkeypatch.setattr("builtins.input", fail_input)

    responses = [
        ModelResponse(
            content=[
                ToolUseBlock(
                    id="toolu_rm",
                    name="bash",
                    input={"command": "rm build/output.txt"},
                )
            ],
            stop_reason="tool_use",
            id="msg_tool",
        ),
        ModelResponse(
            content=[TextBlock("handled denial")],
            stop_reason="end_turn",
            id="msg_final",
        ),
    ]

    def fake_call_llm(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(loop_mod, "call_llm", fake_call_llm)

    service = AgentService(
        workspace=tmp_path,
        approval_registry=ApprovalRegistry(timeout_seconds=0.01),
    )
    session_id = service.create_session()["session"]["session_id"]

    response = service.post_message(session_id, "try destructive command")

    assert response["run"]["status"] == "completed"
    assert response["messages"][-1]["content"][0]["text"] == "handled denial"

    permission_events = [
        event["payload"]
        for event in _events(tmp_path)
        if event["type"] == "permission_decision"
    ]
    assert [event["action"] for event in permission_events] == ["ask", "deny"]
    assert permission_events[1]["source"] == "web_approval_timeout"
    assert permission_events[1]["reason"] == WEB_APPROVAL_EXPIRED_REASON


def test_agent_service_rejects_running_session(tmp_path):
    from coding_agent.web.agent_service import AgentService, SessionRunning

    service = AgentService(workspace=tmp_path)
    session_id = service.create_session()["session"]["session_id"]
    run = service.registry.try_start(session_id)

    try:
        try:
            service.post_message(session_id, "second message")
        except SessionRunning as exc:
            assert exc.details == {"active_run_id": run.run_id}
        else:
            raise AssertionError("expected SessionRunning")
    finally:
        service.registry.complete(run.run_id)


def test_agent_service_fails_run_when_snapshot_cannot_be_saved(
    monkeypatch,
    tmp_path,
):
    from coding_agent.web import agent_service as service_mod
    from coding_agent.web.agent_service import AgentService, PersistenceFailed

    class FakeLoop:
        def run(self, messages, context):
            messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": "not persisted"}],
            })

    service = AgentService(workspace=tmp_path, loop_factory=FakeLoop)
    session_id = service.create_session()["session"]["session_id"]
    monkeypatch.setattr(
        service_mod,
        "save_session_snapshot",
        lambda *args, **kwargs: False,
    )

    with pytest.raises(PersistenceFailed):
        service.post_message(session_id, "hello")

    run = next(iter(service.registry._runs.values()))
    assert run.status == "failed"
    assert service.get_session(session_id)["messages"] == []


def test_agent_service_marks_provider_failure_as_failed(monkeypatch, tmp_path):
    from coding_agent.runtime import loop as loop_mod
    from coding_agent.web.agent_service import (
        AgentExecutionFailed,
        AgentService,
    )

    def fail_call(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(loop_mod, "call_llm", fail_call)
    service = AgentService(workspace=tmp_path)
    session_id = service.create_session()["session"]["session_id"]

    with pytest.raises(AgentExecutionFailed, match="provider exploded"):
        service.post_message(session_id, "trigger provider failure")

    run = next(iter(service.registry._runs.values()))
    assert run.status == "failed"
    assert "provider exploded" in (run.error or "")
    messages = service.get_session(session_id)["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "trigger provider failure"
    assert "provider exploded" in messages[1]["content"][0]["text"]


def test_session_listing_reports_corrupt_session_and_run_state(tmp_path):
    from coding_agent.web.agent_service import AgentService

    sessions_dir = tmp_path / ".agent_sessions"
    sessions_dir.mkdir()
    (sessions_dir / "broken.json").write_text(
        "{not-json",
        encoding="utf-8",
    )
    runs_dir = tmp_path / ".agent_runs"
    runs_dir.mkdir()
    (runs_dir / "run_broken.json").write_text(
        json.dumps({
            "run_id": "../outside",
            "session_id": "session_1",
            "status": "completed",
            "started_at": "2026-01-01T00:00:00Z",
        }),
        encoding="utf-8",
    )

    result = AgentService(workspace=tmp_path).list_sessions()

    assert result["sessions"] == []
    assert len(result["warnings"]) == 2
    assert any("broken.json" in warning for warning in result["warnings"])
    assert any("run_broken.json" in warning for warning in result["warnings"])
    assert not (tmp_path.parent / "outside.json").exists()


def test_web_background_service_dispatches_queued_cron_prompt(tmp_path):
    from coding_agent.cron_scheduler import CronJob, requeue_cron_job
    from coding_agent.runtime.session import load_session
    from coding_agent.web.agent_service import AgentService

    completed = threading.Event()

    class CronLoop:
        def run(self, messages, context):
            del context
            messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": "scheduled response"}],
            })
            completed.set()

    service = AgentService(workspace=tmp_path, loop_factory=CronLoop)
    session_id = service.create_session()["session"]["session_id"]
    requeue_cron_job(
        CronJob(
            id="cron_web",
            cron="* * * * *",
            prompt="inspect scheduled state",
            recurring=False,
            durable=False,
            session_id=session_id,
        ),
        tmp_path,
    )

    service.start_background_services()
    try:
        assert completed.wait(timeout=3)
        deadline = time.time() + 3
        snapshot = {}
        while time.time() < deadline:
            snapshot = load_session(session_id, tmp_path) or {}
            if len(snapshot.get("messages", [])) == 2:
                break
            time.sleep(0.01)
    finally:
        service.shutdown()

    assert len(snapshot["messages"]) == 2
    assert snapshot["messages"][0]["content"] == (
        "[Scheduled] inspect scheduled state"
    )
    assert snapshot["messages"][1]["content"][0]["text"] == (
        "scheduled response"
    )


def test_agent_service_scrubs_secrets_from_event_and_title_previews(tmp_path):
    from coding_agent.web.agent_service import AgentService

    secret = "sk-1234567890abcdef"

    class SecretLoop:
        def run(self, messages, context):
            messages.append({
                "role": "assistant",
                "content": [{
                    "type": "text",
                    "text": f"echo Bearer {secret}",
                }],
            })

    service = AgentService(workspace=tmp_path, loop_factory=SecretLoop)
    session_id = service.create_session()["session"]["session_id"]
    response = service.post_message(
        session_id,
        f"api_key={secret}",
    )

    assert secret not in str(response["session"]["last_user_prompt_preview"])
    persisted_events = _events(tmp_path)
    assert secret not in json.dumps(persisted_events, ensure_ascii=False)
    # The canonical conversation remains intact for the local user; only
    # secondary previews and audit events are redacted.
    assert secret in json.dumps(
        service.get_session(session_id)["messages"],
        ensure_ascii=False,
    )


def test_agent_service_keeps_compaction_context_out_of_display_history(
    tmp_path,
):
    from coding_agent.message_utils import internal_user_message
    from coding_agent.runtime.session import load_session
    from coding_agent.web.agent_service import AgentService

    class CompactingLoop:
        def run(self, messages, context):
            prompt = next(
                message["content"]
                for message in reversed(messages)
                if message.get("role") == "user"
                and not message.get("_internal")
            )
            messages[:] = [
                internal_user_message(
                    "[Compacted]\n\nPRIVATE INTERNAL SUMMARY"
                ),
                internal_user_message(
                    "[Compacted. Continue with summarized context.]"
                ),
                {
                    "role": "assistant",
                    "content": [{
                        "type": "text",
                        "text": f"answer: {prompt}",
                    }],
                },
            ]

    service = AgentService(workspace=tmp_path, loop_factory=CompactingLoop)
    session_id = service.create_session()["session"]["session_id"]

    service.post_message(session_id, "first question")
    service.post_message(session_id, "second question")

    detail = service.get_session(session_id)
    assert detail["display_messages"] == [
        {"role": "user", "content": "first question"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "answer: first question"}],
        },
        {"role": "user", "content": "second question"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "answer: second question"}],
        },
    ]
    assert detail["session"]["message_count"] == 4
    assert "PRIVATE INTERNAL SUMMARY" not in json.dumps(
        detail,
        ensure_ascii=False,
    )

    runtime_snapshot = load_session(session_id, tmp_path)
    assert "PRIVATE INTERNAL SUMMARY" in json.dumps(
        runtime_snapshot["messages"],
        ensure_ascii=False,
    )
    assert "PRIVATE INTERNAL SUMMARY" not in json.dumps(
        runtime_snapshot["display_messages"],
        ensure_ascii=False,
    )


def test_agent_service_keeps_tool_use_turns_out_of_display_history(tmp_path):
    from coding_agent.runtime.session import load_session
    from coding_agent.web.agent_service import AgentService

    class ToolUsingLoop:
        def run(self, messages, context):
            messages.extend([
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "I need to inspect the workspace first.",
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_test",
                            "name": "read_file",
                            "input": {"path": "README.md"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_test",
                            "content": "workspace details",
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "This is the final answer.",
                        },
                    ],
                },
            ])

    service = AgentService(workspace=tmp_path, loop_factory=ToolUsingLoop)
    session_id = service.create_session()["session"]["session_id"]

    response = service.post_message(session_id, "Where is the archive stored?")
    detail = service.get_session(session_id)

    assert response["messages"] == [{
        "role": "assistant",
        "content": [{"type": "text", "text": "This is the final answer."}],
    }]
    assert detail["display_messages"] == [
        {"role": "user", "content": "Where is the archive stored?"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "This is the final answer."}],
        },
    ]
    assert "I need to inspect" in json.dumps(
        load_session(session_id, tmp_path)["messages"],
        ensure_ascii=False,
    )
    assistant_events = [
        event for event in _events(tmp_path)
        if event["type"] == "assistant_message"
    ]
    assert len(assistant_events) == 1
    assert "final answer" in json.dumps(assistant_events[0], ensure_ascii=False)


def test_resumed_approval_run_remains_cancellable():
    from coding_agent.web.run_registry import SessionRunRegistry

    registry = SessionRunRegistry()
    run = registry.try_start("session_test")
    cancellation_event = registry.cancellation_event(run.run_id)

    registry.wait_for_approval(run.run_id, "approval_test")
    registry.resume_after_approval(run.run_id)
    registry.request_cancel(run.session_id, run.run_id)

    assert cancellation_event.is_set()


def test_resolve_approval_resumes_run_before_returning(tmp_path):
    from coding_agent.security.policy import PolicyDecision
    from coding_agent.web.agent_service import AgentService

    service = AgentService(workspace=tmp_path)
    session_id = service.create_session()["session"]["session_id"]
    run = service.registry.try_start(session_id)
    approval = service.approval_registry.create(
        PolicyDecision(
            action="ask",
            tool="bash",
            reason="test approval",
            subject="Remove-Item file.txt",
            tool_use_id="toolu_test",
        ),
        session_id=session_id,
        run_id=run.run_id,
        on_pending=lambda pending: service._run_waiting_for_approval(
            run.run_id,
            pending,
        ),
    )

    assert service.registry.get_run(run.run_id).status == "waiting_approval"

    resolved = service.resolve_approval(
        approval.approval_id,
        "deny",
        session_id=session_id,
        run_id=run.run_id,
    )

    assert resolved["status"] == "denied"
    resumed = service.registry.get_run(run.run_id)
    assert resumed.status == "running"
    assert resumed.pending_approval_id is None


def test_agent_service_hides_legacy_compaction_and_restores_prompt_preview(
    tmp_path,
):
    from coding_agent.runtime.session import create_session
    from coding_agent.web.agent_service import AgentService

    record = create_session(tmp_path)
    payload = json.loads(record.path.read_text(encoding="utf-8"))
    payload.pop("display_messages")
    payload.pop("display_message_count")
    payload["message_count"] = 2
    payload["last_user_prompt_preview"] = {
        "preview": "右侧运行观察是什么作用？",
        "length": 13,
        "truncated": False,
    }
    payload["messages"] = [
        {
            "role": "user",
            "content": "[Compacted]\n\nPRIVATE LEGACY SUMMARY",
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "最终回答"}],
        },
    ]
    record.path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    detail = AgentService(workspace=tmp_path).get_session(record.session_id)

    assert detail["messages"] == [{
        "role": "assistant",
        "content": [{"type": "text", "text": "最终回答"}],
    }]
    assert detail["display_messages"] == [
        {"role": "user", "content": "右侧运行观察是什么作用？"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "最终回答"}],
        },
    ]
    assert detail["session"]["message_count"] == 2
    assert "PRIVATE LEGACY SUMMARY" not in json.dumps(
        detail,
        ensure_ascii=False,
    )


def test_agent_service_recovers_interrupted_approval_run_as_failed(
    tmp_path,
):
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.run_registry import INTERRUPTED_RUN_ERROR

    first = AgentService(workspace=tmp_path)
    session_id = first.create_session()["session"]["session_id"]
    interrupted = first.registry.try_start(session_id)
    first.registry.wait_for_approval(
        interrupted.run_id,
        "approval_before_restart",
    )

    restarted = AgentService(workspace=tmp_path)
    summary = restarted.get_session(session_id)["session"]
    recovered = restarted.get_run(
        session_id,
        interrupted.run_id,
    )["run"]

    assert summary["status"] == "failed"
    assert summary["active_run_id"] is None
    assert recovered["status"] == "failed"
    assert recovered["error"] == INTERRUPTED_RUN_ERROR
    assert recovered["ended_at"] is not None
    assert restarted.approval_registry.list() == []

    events = restarted.event_store.read_events(
        session_id=session_id,
        run_id=interrupted.run_id,
        limit=20,
    )["events"]
    assert [event["type"] for event in events][-2:] == [
        "run_status",
        "run_failed",
    ]

    next_run = restarted.registry.try_start(session_id)
    assert next_run.run_id != interrupted.run_id
    assert restarted.get_session(session_id)["session"]["status"] == (
        "running"
    )
    restarted.registry.cancel(next_run.run_id, "test cleanup")
