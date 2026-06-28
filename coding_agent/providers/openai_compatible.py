"""Minimal OpenAI-compatible chat completions provider."""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib import request

from .base import ModelResponse, TextBlock, ToolUseBlock, Usage

Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="POST")
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        return str(content)
    text = getattr(content, "text", None)
    if text is not None:
        return str(text)
    return str(content)


def _tool_call_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except Exception:
        return {"arguments": str(arguments)}
    return parsed if isinstance(parsed, dict) else {"arguments": parsed}


def _convert_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": (
                tool.get("input_schema")
                or tool.get("inputSchema")
                or {"type": "object", "properties": {}}
            ),
        },
    }


def _convert_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if not isinstance(content, list):
        return {"role": "assistant", "content": _content_text(content)}

    text_parts = []
    tool_calls = []
    for block in content:
        block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if block_type == "tool_use":
            tool_id = block.get("id") if isinstance(block, dict) else getattr(block, "id", "")
            name = block.get("name") if isinstance(block, dict) else getattr(block, "name", "")
            tool_input = block.get("input") if isinstance(block, dict) else getattr(block, "input", {})
            tool_calls.append({
                "id": str(tool_id),
                "type": "function",
                "function": {
                    "name": str(name),
                    "arguments": json.dumps(tool_input or {}),
                },
            })
        else:
            text_parts.append(_content_text(block))

    converted = {"role": "assistant", "content": "\n".join(filter(None, text_parts)) or None}
    if tool_calls:
        converted["tool_calls"] = tool_calls
    return converted


def _convert_user_messages(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return [{"role": "user", "content": _content_text(content)}]

    converted = []
    text_parts = []
    for block in content:
        block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if block_type == "tool_result":
            if text_parts:
                converted.append({"role": "user", "content": "\n".join(text_parts)})
                text_parts = []
            tool_use_id = block.get("tool_use_id") if isinstance(block, dict) else getattr(block, "tool_use_id", "")
            tool_content = block.get("content") if isinstance(block, dict) else getattr(block, "content", "")
            converted.append({
                "role": "tool",
                "tool_call_id": str(tool_use_id),
                "content": _content_text(tool_content),
            })
        else:
            text_parts.append(_content_text(block))

    if text_parts or not converted:
        converted.append({"role": "user", "content": "\n".join(filter(None, text_parts))})
    return converted


def _convert_messages(system: str, messages: list) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    if system:
        converted.append({"role": "system", "content": system})
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            converted.append(_convert_assistant_message(message))
        elif role == "user":
            converted.extend(_convert_user_messages(message))
        else:
            converted.append({"role": role or "user", "content": _content_text(message.get("content"))})
    return converted


def _stop_reason(finish_reason: str | None, has_tool_calls: bool) -> str:
    if has_tool_calls or finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    return finish_reason or "end_turn"


def _response_from_openai(raw: dict[str, Any], requested_model: str) -> ModelResponse:
    choices = raw.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    content: list[Any] = []

    text = message.get("content")
    if text:
        content.append(TextBlock(str(text)))

    tool_calls = message.get("tool_calls") or []
    for tool_call in tool_calls:
        function = tool_call.get("function") or {}
        content.append(ToolUseBlock(
            id=str(tool_call.get("id", "")),
            name=str(function.get("name", "")),
            input=_tool_call_arguments(function.get("arguments")),
        ))

    usage_payload = raw.get("usage") or {}
    usage = Usage(
        input_tokens=usage_payload.get("prompt_tokens"),
        output_tokens=usage_payload.get("completion_tokens"),
    ) if usage_payload else None

    return ModelResponse(
        id=raw.get("id"),
        model=raw.get("model") or requested_model,
        content=content,
        stop_reason=_stop_reason(choice.get("finish_reason"), bool(tool_calls)),
        usage=usage,
    )


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        transport: Transport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport or _post_json

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list,
        tools: list,
        max_tokens: int,
        timeout: float,
    ) -> ModelResponse:
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": _convert_messages(system, messages),
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [_convert_tool(tool) for tool in tools]
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        raw = self.transport(url, headers, payload, timeout)
        return _response_from_openai(raw, model)
