import os
import time
from types import SimpleNamespace

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


def test_anthropic_is_default_provider():
    from coding_agent.providers.anthropic_provider import AnthropicProvider
    from coding_agent.providers.router import provider_from_env

    sentinel_client = object()

    provider = provider_from_env({}, anthropic_client=sentinel_client)

    assert isinstance(provider, AnthropicProvider)
    assert provider.name == "anthropic"
    assert provider.client is sentinel_client


def test_anthropic_provider_strips_internal_message_metadata():
    from coding_agent.providers.anthropic_provider import AnthropicProvider

    captured = {}

    class Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return object()

    provider = AnthropicProvider(
        client=SimpleNamespace(messages=Messages())
    )
    provider.complete(
        model="test-model",
        system="system",
        messages=[{
            "role": "user",
            "content": "[Compacted]\nsummary",
            "_internal": True,
        }],
        tools=[],
        max_tokens=100,
        timeout=1,
    )

    assert captured["messages"] == [{
        "role": "user",
        "content": "[Compacted]\nsummary",
    }]


def test_openai_compatible_provider_builds_chat_completion_request():
    from coding_agent.providers.openai_compatible import OpenAICompatibleProvider

    captured = {}

    def fake_transport(url, headers, payload, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        captured["timeout"] = timeout
        return {
            "id": "chatcmpl_1",
            "model": "openai-model",
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": "I will run it.",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": "{\"command\": \"pwd\"}",
                        },
                    }],
                },
            }],
            "usage": {"prompt_tokens": 7, "completion_tokens": 11},
        }

    provider = OpenAICompatibleProvider(
        base_url="https://openai-compatible.test/v1",
        api_key="secret",
        transport=fake_transport,
    )

    response = provider.complete(
        model="openai-model",
        system="system prompt",
        messages=[{"role": "user", "content": "hello"}],
        tools=[{
            "name": "bash",
            "description": "Run shell commands.",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        }],
        max_tokens=321,
        timeout=12.5,
    )

    assert captured["url"] == (
        "https://openai-compatible.test/v1/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["timeout"] == 12.5
    assert captured["payload"] == {
        "model": "openai-model",
        "messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "hello"},
        ],
        "max_tokens": 321,
        "tools": [{
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Run shell commands.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        }],
    }
    assert response.id == "chatcmpl_1"
    assert response.stop_reason == "tool_use"
    assert response.usage.input_tokens == 7
    assert response.usage.output_tokens == 11
    assert response.content[0].type == "text"
    assert response.content[0].text == "I will run it."
    assert response.content[1].type == "tool_use"
    assert response.content[1].id == "call_1"
    assert response.content[1].name == "bash"
    assert response.content[1].input == {"command": "pwd"}


def test_openai_compatible_provider_converts_tool_results():
    from coding_agent.providers.openai_compatible import OpenAICompatibleProvider

    captured = {}

    def fake_transport(url, headers, payload, timeout):
        captured["messages"] = payload["messages"]
        return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}

    provider = OpenAICompatibleProvider(
        base_url="https://openai-compatible.test/v1",
        api_key="secret",
        transport=fake_transport,
    )

    provider.complete(
        model="openai-model",
        system="",
        messages=[{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": "done",
            }],
        }],
        tools=[],
        max_tokens=100,
        timeout=1,
    )

    assert captured["messages"] == [{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "done",
    }]


def test_call_llm_retry_uses_fallback_model(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.runtime import llm as llm_mod
    import coding_agent.recovery as recovery_mod

    response = SimpleNamespace(
        id="msg_fallback",
        stop_reason="end_turn",
        content=[],
        usage=None,
    )

    class FakeProvider:
        name = "fake-provider"

        def __init__(self):
            self.models = []

        def complete(self, **kwargs):
            self.models.append(kwargs["model"])
            if len(self.models) == 1:
                raise RuntimeError("529 overloaded")
            return response

    provider = FakeProvider()

    monkeypatch.setattr(llm_mod, "get_model_provider", lambda: provider)
    monkeypatch.setattr(
        llm_mod, "assemble_system_prompt", lambda context: "system")
    monkeypatch.setattr(recovery_mod, "FALLBACK_MODEL", "fallback-model")
    monkeypatch.setattr(recovery_mod, "MAX_CONSECUTIVE_529", 1)
    monkeypatch.setattr(recovery_mod, "MAX_RETRIES", 2)
    monkeypatch.setattr(recovery_mod, "retry_delay", lambda attempt: 0)
    monkeypatch.setattr(recovery_mod.time, "sleep", lambda delay: None)

    state = SimpleNamespace(current_model="primary-model", consecutive_529=0)

    result = llm_mod.call_llm([], {}, [], state, 100)

    assert result is response
    assert provider.models == ["primary-model", "fallback-model"]
    assert state.current_model == "fallback-model"


def test_context_compaction_uses_model_provider(monkeypatch):
    from coding_agent import context_compaction as compact_mod

    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="provider summary")])
    captured = {}

    class FakeProvider:
        name = "fake-provider"

        def complete(self, **kwargs):
            captured["kwargs"] = kwargs
            return response

    monkeypatch.setattr(compact_mod, "get_model_provider", lambda: FakeProvider())

    summary = compact_mod.summarize_history([
        {"role": "user", "content": "important context"},
    ])

    assert summary == "provider summary"
    assert captured["kwargs"]["model"] == compact_mod.MODEL
    assert captured["kwargs"]["system"] == ""
    assert captured["kwargs"]["tools"] == []
    assert captured["kwargs"]["max_tokens"] == 2000


def test_spawn_subagent_uses_model_provider(monkeypatch):
    from coding_agent.providers.base import TextBlock
    from coding_agent.tools import subagent as subagent_mod

    captured = {}

    class FakeProvider:
        name = "fake-provider"

        def complete(self, **kwargs):
            captured["kwargs"] = {
                **kwargs,
                "messages": list(kwargs["messages"]),
            }
            return SimpleNamespace(
                content=[TextBlock("subagent summary")],
                stop_reason="end_turn",
            )

    monkeypatch.setattr(
        subagent_mod, "get_model_provider", lambda: FakeProvider())

    result = subagent_mod.spawn_subagent("finish the task")

    assert result == "subagent summary"
    assert captured["kwargs"]["model"] == subagent_mod.MODEL
    assert captured["kwargs"]["system"] == subagent_mod.SUB_SYSTEM
    assert captured["kwargs"]["messages"] == [
        {"role": "user", "content": "finish the task"},
    ]
    assert captured["kwargs"]["tools"] == subagent_mod.SUB_TOOLS
    assert captured["kwargs"]["max_tokens"] == 8000
    assert captured["kwargs"]["timeout"] == subagent_mod.REQUEST_TIMEOUT_SECONDS


def test_teammate_worker_uses_model_provider(monkeypatch):
    from coding_agent.providers.base import TextBlock
    from coding_agent import teams as teams_mod

    class FakeBus:
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

    captured = {}

    class FakeProvider:
        name = "fake-provider"

        def complete(self, **kwargs):
            captured["kwargs"] = {
                **kwargs,
                "messages": list(kwargs["messages"]),
            }
            return SimpleNamespace(
                content=[TextBlock("worker summary")],
                stop_reason="end_turn",
            )

    fake_bus = FakeBus()
    teammate_name = "unit_worker_provider"

    monkeypatch.setattr(teams_mod, "BUS", fake_bus)
    monkeypatch.setattr(
        teams_mod, "get_model_provider", lambda: FakeProvider())
    monkeypatch.setattr(teams_mod, "IDLE_TIMEOUT", 0)
    monkeypatch.setattr(teams_mod, "IDLE_POLL_INTERVAL", 1)
    teams_mod.active_teammates.pop(teammate_name, None)

    try:
        result = teams_mod.spawn_teammate_thread(
            teammate_name, "tester", "finish once")

        deadline = time.time() + 2
        while time.time() < deadline and teammate_name in teams_mod.active_teammates:
            time.sleep(0.01)

        assert result == "Teammate 'unit_worker_provider' spawned as tester"
        assert teammate_name not in teams_mod.active_teammates
    finally:
        teams_mod.active_teammates.pop(teammate_name, None)

    assert captured["kwargs"]["model"] == teams_mod.MODEL
    assert "unit_worker_provider" in captured["kwargs"]["system"]
    assert captured["kwargs"]["messages"][-1] == {
        "role": "user",
        "content": "finish once",
    }
    assert captured["kwargs"]["tools"]
    assert captured["kwargs"]["max_tokens"] == 8000
    assert captured["kwargs"]["timeout"] == teams_mod.REQUEST_TIMEOUT_SECONDS
    result_messages = [msg for msg in fake_bus.sent if msg["type"] == "result"]
    assert result_messages
    assert result_messages[-1]["from"] == teammate_name
    assert result_messages[-1]["to"] == "lead"
    assert "Teammate: unit_worker_provider" in result_messages[-1]["content"]
    assert "Role: tester" in result_messages[-1]["content"]
    assert "Summary:\nworker summary" in result_messages[-1]["content"]
