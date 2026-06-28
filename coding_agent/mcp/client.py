import atexit
import re
import threading
from pathlib import Path

from ..runtime.execution import current_execution_context, execution_workspace
from ..runtime.events import log_event
from .config import load_mcp_configs
from .transport import StdioMCPTransport

# ── MCP System ──

# MCP is modeled as late-bound tools: connect first, then discovered server
# tools are merged into the normal tool pool with mcp__server__tool names.
class MCPClient:
    """Discovers and calls tools on an MCP server (mock for teaching)."""

    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, callable] = {}

    def register(self, tool_defs: list[dict],
                 handlers: dict[str, callable]):
        self.tools = tool_defs
        self._handlers = handlers

    def call_tool(self, tool_name: str, args: dict) -> str:
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return handler(**args)
        except Exception as e:
            return f"MCP error: {e}"


class StdioMCPClient(MCPClient):
    """MCP client backed by a configured stdio JSON-RPC server."""

    def __init__(self, name: str, transport: StdioMCPTransport):
        super().__init__(name)
        self.transport = transport

    def call_tool(self, tool_name: str, args: dict) -> str:
        result = self.transport.call_tool(tool_name, args)
        log_event("mcp_tool_call", {
            "server": self.name,
            "tool": tool_name,
            "ok": not result.startswith("MCP error:"),
        })
        return result


mcp_clients: dict[str, MCPClient] = {}
_mcp_clients_by_workspace: dict[str, dict[str, MCPClient]] = {}
_MCP_CLIENTS_LOCK = threading.RLock()


def current_mcp_clients(
    workspace: str | Path | None = None,
) -> dict[str, MCPClient]:
    with _MCP_CLIENTS_LOCK:
        context = current_execution_context()
        if workspace is None and not context.get("workspace"):
            return mcp_clients
        root = (
            Path(workspace).resolve()
            if workspace is not None
            else execution_workspace()
        )
        return _mcp_clients_by_workspace.setdefault(str(root), {})


def mcp_clients_snapshot(
    workspace: str | Path | None = None,
) -> dict[str, MCPClient]:
    with _MCP_CLIENTS_LOCK:
        return dict(current_mcp_clients(workspace))


_DISALLOWED_CHARS = re.compile(r'[^a-zA-Z0-9_-]')


def normalize_mcp_name(name: str) -> str:
    """Replace non [a-zA-Z0-9_-] with underscore."""
    return _DISALLOWED_CHARS.sub('_', name)


def _mock_server_docs():
    client = MCPClient("docs")
    client.register(
        tool_defs=[
            {"name": "search", "description": "Search documentation. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"query": {"type": "string"}},
                             "required": ["query"]}},
            {"name": "get_version", "description": "Get API version. (readOnly)",
             "inputSchema": {"type": "object", "properties": {},
                             "required": []}},
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        })
    return client


def _mock_server_deploy():
    client = MCPClient("deploy")
    client.register(
        tool_defs=[
            {"name": "trigger",
             "description": "Trigger a deployment. (destructive — requires approval in real CC)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
            {"name": "status", "description": "Check deployment status. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        })
    return client


MOCK_SERVERS = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}


def _log_mcp_connect(
    name: str,
    *,
    ok: bool,
    workspace: str | Path | None = None,
    **payload,
) -> None:
    log_event("mcp_connect", {
        "name": name,
        "server": name,
        "ok": ok,
        **payload,
    }, workspace=workspace)


def _connect_mcp_unlocked(
    name: str,
    workspace: str | Path | None = None,
) -> str:
    clients = current_mcp_clients(workspace)
    if name in clients:
        client = clients[name]
        if isinstance(client, StdioMCPClient):
            process = client.transport.process
            if process is None or process.poll() is not None:
                client.transport.close()
                clients.pop(name, None)
                client = None
        if client is None:
            _log_mcp_connect(
                name,
                ok=False,
                workspace=workspace,
                reconnecting=True,
                error="Existing MCP process is not running; reconnecting.",
            )
        else:
            _log_mcp_connect(
                name,
                ok=True,
                workspace=workspace,
                already_connected=True,
                tool_count=len(getattr(client, "tools", []) or []),
            )
            return f"MCP server '{name}' already connected"
    factory = MOCK_SERVERS.get(name)
    if factory:
        mcp_client = factory()
        clients[name] = mcp_client
        tool_names = [t["name"] for t in mcp_client.tools]
        _log_mcp_connect(
            name,
            ok=True,
            workspace=workspace,
            transport="mock",
            tool_count=len(tool_names),
        )
        print(f"  \033[31m[mcp] connected: {name} -> {tool_names}\033[0m")
        return (f"Connected to MCP server '{name}'. "
                f"Discovered {len(mcp_client.tools)} tools: {', '.join(tool_names)}")

    configs = load_mcp_configs(workspace)
    server_config = configs.get(name)
    if not server_config:
        available = sorted([*MOCK_SERVERS.keys(), *configs.keys()])
        available_text = ", ".join(available) if available else "(none)"
        _log_mcp_connect(
            name,
            ok=False,
            workspace=workspace,
            error=f"Unknown server '{name}'. Available: {available_text}",
        )
        return f"Unknown server '{name}'. Available: {available_text}"

    transport = StdioMCPTransport(server_config)
    tools, error = transport.initialize()
    if error:
        transport.close()
        _log_mcp_connect(
            name,
            ok=False,
            workspace=workspace,
            transport="stdio",
            error=error,
        )
        return error
    mcp_client = StdioMCPClient(name, transport)
    mcp_client.tools = tools
    clients[name] = mcp_client
    tool_names = [t["name"] for t in mcp_client.tools]
    _log_mcp_connect(
        name,
        ok=True,
        workspace=workspace,
        transport="stdio",
        tool_count=len(tool_names),
    )
    print(f"  \033[31m[mcp] connected: {name} -> {tool_names}\033[0m")
    return (f"Connected to MCP server '{name}'. "
            f"Discovered {len(mcp_client.tools)} tools: {', '.join(tool_names)}")


def connect_mcp(name: str, workspace: str | Path | None = None) -> str:
    with _MCP_CLIENTS_LOCK:
        return _connect_mcp_unlocked(name, workspace)


def close_mcp_clients(
    workspace: str | Path | None = None,
) -> None:
    with _MCP_CLIENTS_LOCK:
        context = current_execution_context()
        if workspace is not None or context.get("workspace"):
            root = (
                Path(workspace).resolve()
                if workspace is not None
                else execution_workspace()
            )
            clients = _mcp_clients_by_workspace.pop(str(root), {})
        else:
            clients = mcp_clients
        for client in list(clients.values()):
            transport = getattr(client, "transport", None)
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
        clients.clear()


atexit.register(close_mcp_clients)






def mcp_tool_entries(
    workspace: str | Path | None = None,
) -> tuple[list[dict], dict]:
    tools = []
    handlers = {}
    seen: dict[str, dict] = {}
    with _MCP_CLIENTS_LOCK:
        client_items = list(current_mcp_clients(workspace).items())
    for server_name, mcp_client in client_items:
        safe_server = normalize_mcp_name(server_name)
        for tool_def in mcp_client.tools:
            safe_tool = normalize_mcp_name(tool_def["name"])
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            if prefixed in seen:
                existing = seen[prefixed]
                log_event("mcp_tool_name_collision", {
                    "prefixed_name": prefixed,
                    "server": server_name,
                    "tool": tool_def["name"],
                    "safe_server": safe_server,
                    "safe_tool": safe_tool,
                    "existing_server": existing["server"],
                    "existing_tool": existing["tool"],
                })
                continue
            seen[prefixed] = {
                "server": server_name,
                "tool": tool_def["name"],
            }
            tools.append({
                "name": prefixed,
                "description": tool_def.get("description", ""),
                "input_schema": tool_def.get("inputSchema", {}),
            })
            handlers[prefixed] = (
                lambda *, c=mcp_client, t=tool_def["name"], **kw: c.call_tool(t, kw))
    return tools, handlers
