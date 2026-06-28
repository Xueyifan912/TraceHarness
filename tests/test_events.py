import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


def _read_events(workspace):
    path = workspace / ".agent_events" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _event_text(workspace):
    return (workspace / ".agent_events" / "events.jsonl").read_text()


def test_log_event_writes_workspace_local_jsonl(tmp_path):
    from coding_agent.runtime.events import event_log_path, log_event

    ok = log_event("example", {"value": 1}, workspace=tmp_path)

    assert ok is True
    assert event_log_path(tmp_path) == (
        tmp_path.resolve() / ".agent_events" / "events.jsonl"
    )
    events = _read_events(tmp_path)
    assert events == [{
        "event_id": events[0]["event_id"],
        "ts": events[0]["ts"],
        "type": "example",
        "payload": {"value": 1},
    }]


def test_log_event_swallow_write_errors(tmp_path):
    from coding_agent.runtime.events import log_event

    not_a_directory = tmp_path / "events-root"
    not_a_directory.write_text("blocked")

    assert log_event("example", workspace=not_a_directory) is False


def test_token_stat_fields_are_not_redacted(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.runtime.events import log_event

    assert log_event("stats", {
        "max_tokens": 100,
        "usage": {
            "input_tokens": 3,
            "output_tokens": 5,
        },
    }) is True

    events = _read_events(tmp_path)
    assert events[0]["payload"]["max_tokens"] == 100
    assert events[0]["payload"]["usage"] == {
        "input_tokens": 3,
        "output_tokens": 5,
    }


def test_large_user_prompt_logs_preview_and_length(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.runtime.events import log_user_prompt_submission

    prompt = "a" * 1500

    assert log_user_prompt_submission(prompt) is True

    events = _read_events(tmp_path)
    prompt_payload = events[0]["payload"]["prompt"]
    assert prompt_payload["length"] == 1500
    assert prompt_payload["truncated"] is True
    assert len(prompt_payload["preview"]) < prompt_payload["length"]
    assert prompt not in _event_text(tmp_path)


def test_large_tool_input_logs_preview_and_redacts_sensitive_fields(
        monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.runtime.events import log_tool_call_started

    command = "x" * 1500
    block = SimpleNamespace(
        name="bash",
        id="toolu_big",
        input={
            "command": command,
            "api_key": "super-secret-value",
            "nested": {
                "password": "p@ssw0rd",
                "authorization": "Bearer token-value",
            },
        },
    )

    assert log_tool_call_started(block) is True

    events = _read_events(tmp_path)
    tool_input = events[0]["payload"]["input"]
    assert tool_input["type"] == "object"
    assert tool_input["length"] >= len(command)
    command_payload = tool_input["value"]["command"]
    assert command_payload["length"] == 1500
    assert command_payload["truncated"] is True
    assert len(command_payload["preview"]) < command_payload["length"]
    assert tool_input["value"]["api_key"]["redacted"] is True
    nested = tool_input["value"]["nested"]["value"]
    assert nested["password"]["redacted"] is True
    assert nested["authorization"]["redacted"] is True

    event_text = _event_text(tmp_path)
    assert command not in event_text
    assert "super-secret-value" not in event_text
    assert "p@ssw0rd" not in event_text
    assert "Bearer token-value" not in event_text


def test_memory_append_tool_call_started_omits_content_preview(
        monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.runtime.events import log_tool_call_started

    memory_text = "private memory text " * 40
    block = SimpleNamespace(
        name="memory_append",
        id="toolu_memory",
        input={"content": memory_text},
    )

    assert log_tool_call_started(block) is True

    assert memory_text not in _event_text(tmp_path)
    events = _read_events(tmp_path)
    content = events[0]["payload"]["input"]["value"]["content"]
    assert content == {
        "omitted": True,
        "length": len(memory_text),
        "truncated": len(memory_text) > 500,
    }


def test_memory_append_tool_call_ended_omits_content_preview(
        monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.runtime.events import log_tool_call_ended

    memory_text = "durable private fact " * 40
    block = SimpleNamespace(
        name="memory_append",
        id="toolu_memory_end",
        input={"content": memory_text},
    )

    assert log_tool_call_ended(block, "Appended memory", "completed") is True

    assert memory_text not in _event_text(tmp_path)
    events = _read_events(tmp_path)
    payload = events[0]["payload"]
    content = payload["input"]["value"]["content"]
    assert payload["status"] == "completed"
    assert content["omitted"] is True
    assert content["length"] == len(memory_text)
    assert "preview" not in content


def test_regular_tool_input_still_has_bounded_preview(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.runtime.events import log_tool_call_started

    command = "echo visible preview"
    block = SimpleNamespace(
        name="bash",
        id="toolu_bash",
        input={"command": command},
    )

    assert log_tool_call_started(block) is True

    events = _read_events(tmp_path)
    command_payload = events[0]["payload"]["input"]["value"]["command"]
    assert command_payload == {
        "preview": command,
        "length": len(command),
        "truncated": False,
    }


def test_call_llm_logs_start_and_end_without_real_api(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.runtime import llm as llm_mod

    response = SimpleNamespace(
        id="msg_1",
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="done")],
        usage=SimpleNamespace(input_tokens=3, output_tokens=5),
    )
    captured = {}

    class FakeProvider:
        name = "fake-provider"

        def complete(self, **kwargs):
            captured["kwargs"] = kwargs
            return response

    def fake_with_retry(fn, state):
        return fn()

    monkeypatch.setattr(llm_mod, "get_model_provider", lambda: FakeProvider())
    monkeypatch.setattr(llm_mod, "with_retry", fake_with_retry)
    monkeypatch.setattr(
        llm_mod, "assemble_system_prompt", lambda context: "system")

    state = SimpleNamespace(current_model="test-model")
    result = llm_mod.call_llm(
        [{"role": "user", "content": "hi"}],
        {},
        [{"name": "bash"}],
        state,
        123,
    )

    assert result is response
    assert captured["kwargs"]["model"] == "test-model"
    assert captured["kwargs"]["max_tokens"] == 123
    events = _read_events(tmp_path)
    assert [event["type"] for event in events] == [
        "llm_call_started",
        "llm_call_ended",
    ]
    assert events[0]["payload"]["message_count"] == 1
    assert events[0]["payload"]["provider"] == "fake-provider"
    assert events[0]["payload"]["llm_call_id"]
    assert (
        events[1]["payload"]["llm_call_id"]
        == events[0]["payload"]["llm_call_id"]
    )
    assert events[1]["payload"]["stop_reason"] == "end_turn"
    assert events[1]["payload"]["provider"] == "fake-provider"
    assert events[1]["payload"]["usage"] == {
        "input_tokens": 3,
        "output_tokens": 5,
    }


def test_call_llm_logs_failure_without_real_api(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.runtime import llm as llm_mod

    class FakeProvider:
        name = "failing-provider"

        def complete(self, **kwargs):
            raise RuntimeError("provider boom")

    retry_calls = []

    def fake_with_retry(fn, state):
        retry_calls.append(state.current_model)
        return fn()

    monkeypatch.setattr(llm_mod, "get_model_provider", lambda: FakeProvider())
    monkeypatch.setattr(llm_mod, "with_retry", fake_with_retry)
    monkeypatch.setattr(
        llm_mod, "assemble_system_prompt", lambda context: "system")

    state = SimpleNamespace(current_model="test-model")
    with pytest.raises(RuntimeError, match="provider boom"):
        llm_mod.call_llm(
            [{"role": "user", "content": "hi"}],
            {},
            [],
            state,
            123,
        )

    assert retry_calls == ["test-model"]
    events = _read_events(tmp_path)
    assert [event["type"] for event in events] == [
        "llm_call_started",
        "llm_call_failed",
    ]
    failure = events[1]["payload"]
    assert failure["llm_call_id"] == events[0]["payload"]["llm_call_id"]
    assert failure["provider"] == "failing-provider"
    assert failure["error_type"] == "RuntimeError"
    assert failure["message"]["preview"] == "provider boom"


def test_loop_logs_denied_tool_and_final_stop(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.runtime import loop as loop_mod

    responses = iter([
        SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(
                    type="tool_use",
                    name="bash",
                    id="toolu_1",
                    input={"command": "sudo reboot"},
                )
            ],
        ),
        SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="done")],
        ),
    ])

    def fake_trigger_hooks(event, *args):
        if event == "PreToolUse":
            return "Permission denied by test"
        return None

    monkeypatch.setattr(loop_mod, "assemble_tool_pool", lambda: ([], {}))
    monkeypatch.setattr(loop_mod, "call_llm", lambda *args: next(responses))
    monkeypatch.setattr(loop_mod, "trigger_hooks", fake_trigger_hooks)

    messages = [{"role": "user", "content": "run it"}]
    loop_mod.AgentLoop().run(messages, {})

    events = _read_events(tmp_path)
    assert [event["type"] for event in events] == [
        "tool_call_started",
        "permission_denied",
        "tool_call_ended",
        "final_stop",
    ]
    assert events[1]["payload"]["reason"] == "Permission denied by test"
    assert events[2]["payload"]["status"] == "denied"
    assert events[3]["payload"]["reason"] == "end_turn"
    assert events[3]["payload"]["tool_result_count"] == 1


def test_loop_logs_final_stop_when_tool_handler_raises(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.runtime import loop as loop_mod

    response = SimpleNamespace(
        stop_reason="tool_use",
        content=[
            SimpleNamespace(
                type="tool_use",
                name="explode",
                id="toolu_error",
                input={},
            )
        ],
    )

    def explode():
        raise RuntimeError("tool exploded")

    monkeypatch.setattr(loop_mod, "assemble_tool_pool",
                        lambda: ([], {"explode": explode}))
    monkeypatch.setattr(loop_mod, "call_llm", lambda *args: response)
    monkeypatch.setattr(loop_mod, "trigger_hooks", lambda *args: None)

    messages = [{"role": "user", "content": "run it"}]
    with pytest.raises(RuntimeError, match="tool exploded"):
        loop_mod.AgentLoop().run(messages, {})

    events = _read_events(tmp_path)
    assert [event["type"] for event in events] == [
        "tool_call_started",
        "tool_call_ended",
        "final_stop",
    ]
    assert events[1]["payload"]["status"] == "failed"
    assert events[2]["payload"]["reason"] == "tool_error"
    assert events[2]["payload"]["tool"] == "explode"
    assert events[2]["payload"]["tool_use_id"] == "toolu_error"
