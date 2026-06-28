"""Minimal stdio MCP JSON-RPC transport."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any

from .config import MCPServerConfig

MCP_PROTOCOL_VERSION = "2024-11-05"
REQUEST_TIMEOUT_SECONDS = 10


@dataclass
class StdioMCPTransport:
    config: MCPServerConfig
    process: subprocess.Popen | None = None
    _next_id: int = 1
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stderr_lock: threading.Lock = field(default_factory=threading.Lock)
    _stderr_chunks: list[bytes] = field(default_factory=list)

    def start(self) -> str | None:
        if self.process and self.process.poll() is None:
            return None
        env = os.environ.copy()
        env.update(self.config.env)
        try:
            self.process = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self.config.workspace,
            )
            self._start_stderr_drain()
        except Exception as e:
            return f"MCP error: failed to start server '{self.config.name}': {e}"
        return None

    def initialize(self) -> tuple[list[dict], str | None]:
        start_error = self.start()
        if start_error:
            return [], start_error
        _, error = self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "coding-agent-harness", "version": "0.1"},
        })
        if error:
            return [], error
        notification_error = self._write_message({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        })
        if notification_error:
            return [], notification_error
        tools_result, error = self._request("tools/list", {})
        if error:
            return [], error
        tools = tools_result.get("tools", []) if isinstance(tools_result, dict) else []
        if not isinstance(tools, list):
            return [], "MCP error: tools/list returned invalid tools"
        return [tool for tool in tools if isinstance(tool, dict)], None

    def call_tool(self, tool_name: str, args: dict) -> str:
        result, error = self._request("tools/call", {
            "name": tool_name,
            "arguments": args or {},
        })
        if error:
            return error
        return _result_to_text(result)

    def close(self):
        process = self.process
        self.process = None
        if not process or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _start_stderr_drain(self) -> None:
        process = self.process
        if not process or not process.stderr:
            return

        def drain() -> None:
            while True:
                try:
                    chunk = process.stderr.read1(1024)
                except Exception:
                    return
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr_chunks.append(chunk)
                    joined = b"".join(self._stderr_chunks)
                    self._stderr_chunks = [joined[-8192:]]

        threading.Thread(
            target=drain,
            name=f"mcp-stderr-{self.config.name}",
            daemon=True,
        ).start()

    def _stderr_preview(self) -> str:
        with self._stderr_lock:
            data = b"".join(self._stderr_chunks)[-500:]
        if not data:
            return ""
        return f": {data.decode('utf-8', errors='replace').strip()}"

    def _request(self, method: str, params: dict[str, Any]) -> tuple[Any, str | None]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            error = self._write_message(payload)
            if error:
                return None, error
            while True:
                message, error = self._read_message_with_timeout()
                if error:
                    return None, error
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    return None, f"MCP error: {_error_to_text(message['error'])}"
                return message.get("result", {}), None

    def _read_message_with_timeout(self) -> tuple[dict[str, Any], str | None]:
        messages: queue.Queue[tuple[dict[str, Any], str | None]] = queue.Queue(maxsize=1)

        def reader():
            messages.put(self._read_message())

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            return messages.get(timeout=REQUEST_TIMEOUT_SECONDS)
        except queue.Empty:
            self.close()
            return {}, (
                f"MCP error: request timed out after "
                f"{REQUEST_TIMEOUT_SECONDS:g}s"
            )

    def _write_message(self, payload: dict[str, Any]) -> str | None:
        if not self.process or self.process.poll() is not None:
            return f"MCP error: server '{self.config.name}' is not running"
        if not self.process.stdin:
            return f"MCP error: server '{self.config.name}' stdin unavailable"
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.process.stdin.write(body + b"\n")
            self.process.stdin.flush()
            return None
        except Exception as e:
            return f"MCP error: failed to write request: {e}"

    def _read_message(self) -> tuple[dict[str, Any], str | None]:
        if not self.process or not self.process.stdout:
            return {}, f"MCP error: server '{self.config.name}' stdout unavailable"
        try:
            while True:
                line = self.process.stdout.readline()
                if line == b"":
                    stderr = self._stderr_preview()
                    return {}, f"MCP error: server closed stdout{stderr}"
                if line.strip():
                    break
            message = json.loads(line.decode("utf-8"))
            if not isinstance(message, dict):
                return {}, "MCP error: invalid JSON-RPC response"
            return message, None
        except Exception as e:
            return {}, f"MCP error: failed to read response: {e}"

def _error_to_text(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        if message and code is not None:
            return f"{message} (code {code})"
        if message:
            return str(message)
    return str(error)


def _result_to_text(result: Any) -> str:
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            if parts:
                return "\n".join(parts)
        if "structuredContent" in result:
            return json.dumps(result["structuredContent"], ensure_ascii=False)
        if "content" in result:
            return str(result["content"])
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)
