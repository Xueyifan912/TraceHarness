"""Anthropic provider adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AnthropicProvider:
    client: Any
    name: str = "anthropic"

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
        provider_messages = [
            {
                "role": message.get("role", "user"),
                "content": message.get("content", ""),
            }
            for message in messages
            if isinstance(message, dict)
        ]
        return self.client.messages.create(
            model=model,
            system=system,
            messages=provider_messages,
            tools=tools,
            max_tokens=max_tokens,
            timeout=timeout,
        )
