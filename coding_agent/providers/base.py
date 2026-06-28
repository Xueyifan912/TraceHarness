"""Shared provider protocol and Anthropic-compatible response shapes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class ModelResponse:
    content: list[Any]
    stop_reason: str
    id: str | None = None
    model: str | None = None
    usage: Usage | None = None


class ModelProvider(Protocol):
    name: str

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list,
        tools: list,
        max_tokens: int,
        timeout: float,
    ) -> Any:
        """Return a response compatible with the agent loop."""
