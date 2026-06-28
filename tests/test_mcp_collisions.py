import json
import os

import pytest

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def clear_mcp_clients():
    from coding_agent.mcp.client import mcp_clients

    mcp_clients.clear()
    yield
    mcp_clients.clear()


def _client(name, tools, handlers):
    from coding_agent.mcp.client import MCPClient

    client = MCPClient(name)
    client.register(tools, handlers)
    return client


def _read_events(workspace):
    event_path = workspace / ".agent_events" / "events.jsonl"
    return [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
    ]


def test_same_server_normalized_tool_collision_does_not_overwrite(
        monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.mcp.client import mcp_clients, mcp_tool_entries

    mcp_clients["collision"] = _client(
        "collision",
        [
            {"name": "alpha.tool", "description": "first",
             "inputSchema": {"type": "object", "properties": {}, "required": []}},
            {"name": "alpha/tool", "description": "second",
             "inputSchema": {"type": "object", "properties": {}, "required": []}},
        ],
        {
            "alpha.tool": lambda: "first handler",
            "alpha/tool": lambda: "second handler",
        },
    )

    tools, handlers = mcp_tool_entries()

    assert [tool["name"] for tool in tools] == ["mcp__collision__alpha_tool"]
    assert set(handlers) == {"mcp__collision__alpha_tool"}
    assert handlers["mcp__collision__alpha_tool"]() == "first handler"

    collision_events = [
        event for event in _read_events(tmp_path)
        if event["type"] == "mcp_tool_name_collision"
    ]
    assert len(collision_events) == 1
    assert collision_events[0]["payload"] == {
        "prefixed_name": "mcp__collision__alpha_tool",
        "server": "collision",
        "tool": "alpha/tool",
        "safe_server": "collision",
        "safe_tool": "alpha_tool",
        "existing_server": "collision",
        "existing_tool": "alpha.tool",
    }


def test_cross_server_normalized_name_collision_does_not_overwrite(
        monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.mcp.client import mcp_clients, mcp_tool_entries

    tool = {"name": "run", "description": "run",
            "inputSchema": {"type": "object", "properties": {}, "required": []}}
    mcp_clients["team.docs"] = _client(
        "team.docs", [tool], {"run": lambda: "first server"})
    mcp_clients["team/docs"] = _client(
        "team/docs", [tool], {"run": lambda: "second server"})

    tools, handlers = mcp_tool_entries()

    assert [tool["name"] for tool in tools] == ["mcp__team_docs__run"]
    assert set(handlers) == {"mcp__team_docs__run"}
    assert handlers["mcp__team_docs__run"]() == "first server"


def test_non_conflicting_mock_mcp_tools_still_register():
    from coding_agent.mcp.client import connect_mcp
    from coding_agent.tools.registry import assemble_tool_pool

    connect_mcp("docs")

    tools, handlers = assemble_tool_pool()
    names = {tool["name"] for tool in tools}
    assert "mcp__docs__search" in names
    assert "mcp__docs__get_version" in names
    assert handlers["mcp__docs__search"](query="python") == (
        "[docs] Found 3 results for 'python'")
