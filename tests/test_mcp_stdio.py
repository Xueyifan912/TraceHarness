import json
import os
import sys
import threading
import time

import pytest

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


FAKE_SERVER = r'''
import json
import sys


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        line = line.decode("ascii").strip()
        if not line:
            break
        key, _, value = line.partition(":")
        headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def write_message(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        write_message({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1.0"},
            },
        })
    elif method == "tools/list":
        write_message({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": "echo-tool",
                        "description": "Echo fake text.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                    {
                        "name": "fail-tool",
                        "description": "Return a fake MCP error.",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    },
                ]
            },
        })
    elif method == "tools/call":
        params = message.get("params", {})
        name = params.get("name")
        if name == "fail-tool":
            write_message({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": "fake failure"},
            })
        else:
            text = params.get("arguments", {}).get("text", "")
            write_message({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"echo: {text}"}]
                },
            })
    else:
        write_message({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"unknown method: {method}"},
        })
'''


def _close_clients():
    from coding_agent.mcp.client import mcp_clients

    for client in list(mcp_clients.values()):
        transport = getattr(client, "transport", None)
        process = getattr(transport, "process", None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except Exception:
                process.kill()
    mcp_clients.clear()


@pytest.fixture(autouse=True)
def clear_mcp_clients():
    _close_clients()
    yield
    _close_clients()


def _write_fake_server(tmp_path):
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(FAKE_SERVER, encoding="utf-8")
    return server


def _write_mcp_config(tmp_path, server):
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "servers": {
            "fake": {
                "command": sys.executable,
                "args": [str(server)],
                "env": {"FAKE_MCP_ENV": "1"},
            }
        }
    }), encoding="utf-8")


def test_mock_mcp_still_available():
    from coding_agent.mcp.client import connect_mcp

    result = connect_mcp("docs")

    assert "Connected to MCP server 'docs'" in result
    assert "search" in result


def test_concurrent_connect_initializes_mock_server_once(monkeypatch, tmp_path):
    from coding_agent.mcp import client as client_mod

    server_name = "concurrent-test"
    barrier = threading.Barrier(2)
    factory_calls = 0
    factory_lock = threading.Lock()

    def factory():
        nonlocal factory_calls
        with factory_lock:
            factory_calls += 1
        time.sleep(0.05)
        client = client_mod.MCPClient(server_name)
        client.register(
            tool_defs=[{
                "name": "echo",
                "description": "Echo.",
                "inputSchema": {"type": "object", "properties": {}},
            }],
            handlers={"echo": lambda: "echo"},
        )
        return client

    monkeypatch.setitem(client_mod.MOCK_SERVERS, server_name, factory)
    results = []

    def worker():
        barrier.wait(timeout=2)
        results.append(client_mod.connect_mcp(server_name, tmp_path))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert factory_calls == 1
    assert len(results) == 2
    assert sum("already connected" in result for result in results) == 1
    assert len(client_mod.current_mcp_clients(tmp_path)) == 1
    client_mod.close_mcp_clients(tmp_path)


def test_stdio_mcp_connect_discovers_tools(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.mcp.client import connect_mcp
    from coding_agent.tools.registry import assemble_tool_pool

    _write_mcp_config(tmp_path, _write_fake_server(tmp_path))

    result = connect_mcp("fake")

    assert "Connected to MCP server 'fake'" in result
    assert "echo-tool" in result
    tools, handlers = assemble_tool_pool()
    names = {tool["name"] for tool in tools}
    assert "mcp__fake__echo-tool" in names
    assert "mcp__fake__echo-tool" in handlers


def test_stdio_mcp_discovered_tool_can_be_called(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.mcp.client import connect_mcp
    from coding_agent.tools.registry import assemble_tool_pool

    _write_mcp_config(tmp_path, _write_fake_server(tmp_path))
    connect_mcp("fake")
    _, handlers = assemble_tool_pool()

    output = handlers["mcp__fake__echo-tool"](text="hello")

    assert output == "echo: hello"


def test_stdio_mcp_tool_error_is_readable(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.mcp.client import connect_mcp
    from coding_agent.tools.registry import assemble_tool_pool

    _write_mcp_config(tmp_path, _write_fake_server(tmp_path))
    connect_mcp("fake")
    _, handlers = assemble_tool_pool()

    output = handlers["mcp__fake__fail-tool"]()

    assert output == "MCP error: fake failure (code -32000)"


def test_unknown_mcp_server_returns_readable_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.mcp.client import connect_mcp

    result = connect_mcp("missing")

    assert result.startswith("Unknown server 'missing'. Available:")
    assert "docs" in result
    assert "deploy" in result


def test_stdio_mcp_process_start_failure_is_readable(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.mcp.client import connect_mcp

    (tmp_path / ".mcp.json").write_text(json.dumps({
        "servers": {
            "broken": {
                "command": "definitely-not-a-real-mcp-command",
                "args": [],
            }
        }
    }), encoding="utf-8")

    result = connect_mcp("broken")

    assert result.startswith("MCP error: failed to start server 'broken':")


def test_stdio_mcp_timeout_is_readable(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.mcp import transport as transport_mod
    from coding_agent.mcp.client import connect_mcp, mcp_clients

    monkeypatch.setattr(transport_mod, "REQUEST_TIMEOUT_SECONDS", 0.1)
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "servers": {
            "slow": {
                "command": sys.executable,
                "args": ["-c", "import time; time.sleep(30)"],
            }
        }
    }), encoding="utf-8")

    result = connect_mcp("slow")

    assert result == "MCP error: request timed out after 0.1s"
    assert "slow" not in mcp_clients


def test_stdio_mcp_drains_large_stderr_without_deadlock(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from coding_agent.mcp.client import connect_mcp

    noisy_server = tmp_path / "noisy_mcp_server.py"
    noisy_server.write_text(
        FAKE_SERVER.replace(
            "\nwhile True:\n    message = read_message()",
            "sys.stderr.write('x' * 200000)\n"
            "sys.stderr.flush()\n\n"
            "while True:\n    message = read_message()",
            1,
        ),
        encoding="utf-8",
    )
    _write_mcp_config(tmp_path, noisy_server)

    result = connect_mcp("fake")

    assert "Connected to MCP server 'fake'" in result
