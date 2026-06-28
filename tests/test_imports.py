import os

os.environ.setdefault("MODEL_ID", "test-model")


def test_tool_pool_contains_builtin_tools():
    from coding_agent.tools.registry import assemble_tool_pool

    tools, handlers = assemble_tool_pool()
    names = {tool["name"] for tool in tools}
    assert "bash" in names
    assert "todo_write" in names
    assert "connect_mcp" in names
    assert "bash" in handlers


def test_mcp_tools_are_added_after_connect():
    from coding_agent.mcp.client import connect_mcp
    from coding_agent.tools.registry import assemble_tool_pool

    connect_mcp("docs")
    tools, handlers = assemble_tool_pool()
    names = {tool["name"] for tool in tools}
    assert "mcp__docs__search" in names
    assert "mcp__docs__search" in handlers
