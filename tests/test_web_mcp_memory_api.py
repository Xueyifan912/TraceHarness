import json
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def clear_mcp_clients():
    from coding_agent.mcp.client import mcp_clients

    for client in list(mcp_clients.values()):
        transport = getattr(client, "transport", None)
        close = getattr(transport, "close", None)
        if close:
            close()
    mcp_clients.clear()
    yield
    for client in list(mcp_clients.values()):
        transport = getattr(client, "transport", None)
        close = getattr(transport, "close", None)
        if close:
            close()
    mcp_clients.clear()


def _client(tmp_path, *, raise_server_exceptions=True):
    from coding_agent.web.agent_service import AgentService
    from coding_agent.web.app import create_app

    service = AgentService(workspace=tmp_path)
    return TestClient(
        create_app(service=service),
        raise_server_exceptions=raise_server_exceptions,
    )


def _events(tmp_path):
    path = tmp_path / ".agent_events" / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _mcp_client(name, tools, handlers):
    from coding_agent.mcp.client import MCPClient

    client = MCPClient(name)
    client.register(tools, handlers)
    return client


def test_mcp_status_returns_mock_configured_connected_and_errors(tmp_path):
    from coding_agent.mcp import client as mcp_client_mod
    from coding_agent.runtime.events import log_event

    client = _client(tmp_path)
    (tmp_path / ".mcp.json").write_text(
        json.dumps({
            "servers": {
                "local-tools": {
                    "command": "python",
                    "args": ["server.py"],
                    "env": {
                        "TOKEN": "super-secret-token",
                        "PROJECT_ID": "visible-key-only",
                    },
                }
            }
        }),
        encoding="utf-8",
    )
    mcp_client_mod.current_mcp_clients(tmp_path)["docs"] = (
        mcp_client_mod.MOCK_SERVERS["docs"]()
    )
    log_event(
        "mcp_connect",
        {"server": "broken", "name": "broken", "ok": False, "error": "failed"},
        workspace=tmp_path,
    )

    response = client.get("/api/mcp/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mock_servers"] == ["deploy", "docs"]
    assert payload["configured_servers"] == [{
        "name": "local-tools",
        "transport": "stdio",
        "command": "python",
        "args": ["server.py"],
        "env_keys": ["PROJECT_ID", "TOKEN"],
        "configured": True,
    }]
    assert "super-secret-token" not in response.text
    assert "visible-key-only" not in response.text
    assert payload["connected_servers"][0]["name"] == "docs"
    assert payload["connected_servers"][0]["transport"] == "mock"
    assert payload["connected_servers"][0]["tool_count"] == 2
    assert payload["errors"][0]["type"] == "mcp_connect"
    assert payload["errors"][0]["server"] == "broken"


def test_mcp_connect_successfully_connects_mock_server(tmp_path):
    client = _client(tmp_path)

    response = client.post("/api/mcp/connect", json={"name": "docs"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert "Connected to MCP server 'docs'" in payload["message"]
    assert payload["server"]["name"] == "docs"
    assert payload["server"]["transport"] == "mock"
    assert payload["server"]["tool_count"] == 2
    assert [tool["name"] for tool in payload["server"]["tools"]] == [
        "mcp__docs__search",
        "mcp__docs__get_version",
    ]

    repeated = client.post("/api/mcp/connect", json={"name": "docs"})
    assert repeated.status_code == 200
    assert "already connected" in repeated.json()["message"]

    connect_events = [
        event for event in _events(tmp_path)
        if event["type"] == "mcp_connect"
    ]
    assert connect_events[-1]["payload"]["ok"] is True
    assert connect_events[-1]["payload"]["server"] == "docs"
    assert connect_events[-1]["payload"]["tool_count"] == 2


def test_mcp_connect_missing_server_returns_error_envelope(tmp_path):
    client = _client(tmp_path, raise_server_exceptions=False)

    response = client.post("/api/mcp/connect", json={"name": "missing"})

    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "mcp_server_not_found"
    assert "Unknown server 'missing'" in payload["error"]["message"]

    status = client.get("/api/mcp/status").json()
    assert status["errors"][-1]["server"] == "missing"


def test_tools_api_returns_builtin_and_mcp_discovered_tools(tmp_path):
    client = _client(tmp_path)

    initial = client.get("/api/tools")

    assert initial.status_code == 200
    tools = initial.json()["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    assert by_name["bash"]["source"] == "builtin"
    assert by_name["bash"]["server"] is None
    assert by_name["memory_append"]["input_schema"]["properties"] == {
        "content": {"type": "string"}
    }

    connected = client.post("/api/mcp/connect", json={"name": "docs"})
    assert connected.status_code == 200
    after = client.get("/api/tools").json()["tools"]
    by_name = {tool["name"]: tool for tool in after}
    assert by_name["mcp__docs__search"]["source"] == "mcp"
    assert by_name["mcp__docs__search"]["server"] == "docs"
    assert by_name["mcp__docs__search"]["input_schema"]["required"] == ["query"]


def test_tools_api_mcp_collision_is_visible_in_status(tmp_path):
    from coding_agent.mcp.client import current_mcp_clients

    client = _client(tmp_path)
    mcp_clients = current_mcp_clients(tmp_path)
    tool = {
        "name": "run",
        "description": "run",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    }
    mcp_clients["team.docs"] = _mcp_client(
        "team.docs",
        [tool],
        {"run": lambda: "first"},
    )
    mcp_clients["team/docs"] = _mcp_client(
        "team/docs",
        [tool],
        {"run": lambda: "second"},
    )

    response = client.get("/api/tools")
    assert response.status_code == 200
    names = [tool["name"] for tool in response.json()["tools"]]
    assert names.count("mcp__team_docs__run") == 1

    status = client.get("/api/mcp/status").json()
    collision = [
        error for error in status["errors"]
        if error["type"] == "mcp_tool_name_collision"
    ][-1]
    assert collision["prefixed_name"] == "mcp__team_docs__run"
    assert collision["server"] == "team/docs"


def test_memory_api_uses_fixed_file_rejects_path_and_audits_safely(tmp_path):
    client = _client(tmp_path)
    outside = tmp_path / "outside.md"
    secret = "private persistent fact"

    response = client.post(
        "/api/memory/append",
        json={"content": secret},
    )

    assert response.status_code == 200
    payload = response.json()
    memory_path = tmp_path / ".memory" / "MEMORY.md"
    assert payload["ok"] is True
    assert payload["length"] == len(secret)
    assert payload["max_length"] == 20 * 1024
    assert payload["memory"]["path"] == str(memory_path)
    assert memory_path.read_text(encoding="utf-8") == f"- {secret}\n"
    assert not outside.exists()

    memory = client.get("/api/memory")
    assert memory.status_code == 200
    assert memory.json()["path"] == str(memory_path)
    assert memory.json()["exists"] is True

    rejected_path = client.post(
        "/api/memory/append",
        json={"content": "bad path", "path": str(outside)},
    )
    assert rejected_path.status_code == 422
    assert rejected_path.json()["error"]["code"] == "validation_error"
    assert not outside.exists()

    event_text = (tmp_path / ".agent_events" / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert secret not in event_text
    memory_events = [
        event for event in _events(tmp_path)
        if event["type"] == "memory_append"
    ]
    payload = memory_events[-1]["payload"]
    assert payload["content_length"] == len(secret)
    assert payload["content_omitted"] is True
    assert memory_events[-1]["source"] == "web"
    assert memory_events[-1].get("session_id") is None


def test_memory_append_with_session_id_is_visible_in_session_events(tmp_path):
    client = _client(tmp_path)
    session_id = client.post("/api/sessions", json={}).json()["session"]["session_id"]
    secret = "session scoped private memory"

    response = client.post(
        "/api/memory/append",
        params={"session_id": session_id},
        json={"content": secret},
    )

    assert response.status_code == 200
    session_events = client.get(f"/api/sessions/{session_id}/events")
    assert session_events.status_code == 200
    memory_events = [
        event for event in session_events.json()["events"]
        if event["type"] == "memory_append"
    ]
    assert memory_events
    event = memory_events[-1]
    assert event["source"] == "web"
    assert event["session_id"] == session_id
    assert event["payload"]["content_length"] == len(secret)

    event_text = (tmp_path / ".agent_events" / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert secret not in event_text


def test_memory_append_with_missing_session_returns_error_envelope(tmp_path):
    client = _client(tmp_path, raise_server_exceptions=False)

    response = client.post(
        "/api/memory/append",
        params={"session_id": "missing-session"},
        json={"content": "memory"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_not_found"


def test_memory_response_length_is_returned_content_char_count(tmp_path):
    client = _client(tmp_path)
    memory_path = tmp_path / ".memory" / "MEMORY.md"
    memory_path.parent.mkdir(parents=True)
    content = "中文memory\n"
    memory_path.write_bytes(content.encode("utf-8"))

    response = client.get("/api/memory")

    assert response.status_code == 200
    payload = response.json()
    assert payload["content"] == content
    assert payload["length"] == len(content)
    assert payload["size_bytes"] == len(content.encode("utf-8"))
    assert payload["size_bytes"] != payload["length"]


def test_memory_api_append_empty_and_too_large_are_bounded(tmp_path):
    client = _client(tmp_path)

    empty = client.post("/api/memory/append", json={"content": "   "})
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "invalid_memory_content"

    too_large = client.post(
        "/api/memory/append",
        json={"content": "x" * (20 * 1024 + 1)},
    )
    assert too_large.status_code == 422
    assert too_large.json()["error"]["code"] == "validation_error"


def test_memory_store_failure_returns_error_envelope(monkeypatch, tmp_path):
    from coding_agent.web import status_service as status_service_mod

    monkeypatch.setattr(
        status_service_mod,
        "append_memory",
        lambda content, workspace: "Error: simulated write failure",
    )
    client = _client(tmp_path, raise_server_exceptions=False)

    response = client.post(
        "/api/memory/append",
        json={"content": "durable fact"},
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "memory_append_failed",
            "message": "Memory could not be appended.",
            "details": {"error_type": "memory_store_error"},
        }
    }
