"""Provider selection for model calls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .. import config
from .anthropic_provider import AnthropicProvider
from .base import ModelProvider
from .openai_compatible import OpenAICompatibleProvider, Transport


def _normalise_provider_name(name: str | None) -> str:
    return (name or "anthropic").strip().lower().replace("_", "-")


def provider_from_env(
    env: Mapping[str, str | None],
    *,
    anthropic_client: Any | None = None,
    openai_transport: Transport | None = None,
) -> ModelProvider:
    provider_name = _normalise_provider_name(env.get("MODEL_PROVIDER"))

    if provider_name in ("anthropic", "claude"):
        return AnthropicProvider(anthropic_client or config.client)

    if provider_name in ("openai-compatible", "openai"):
        base_url = env.get("OPENAI_COMPATIBLE_BASE_URL")
        api_key = env.get("OPENAI_COMPATIBLE_API_KEY")
        if not base_url:
            raise RuntimeError("OPENAI_COMPATIBLE_BASE_URL is required")
        if not api_key:
            raise RuntimeError("OPENAI_COMPATIBLE_API_KEY is required")
        return OpenAICompatibleProvider(
            base_url=base_url,
            api_key=api_key,
            transport=openai_transport,
        )

    raise RuntimeError(f"Unknown MODEL_PROVIDER '{env.get('MODEL_PROVIDER')}'")


def get_model_provider() -> ModelProvider:
    return provider_from_env({
        "MODEL_PROVIDER": config.MODEL_PROVIDER,
        "OPENAI_COMPATIBLE_BASE_URL": config.OPENAI_COMPATIBLE_BASE_URL,
        "OPENAI_COMPATIBLE_API_KEY": config.OPENAI_COMPATIBLE_API_KEY,
    })
