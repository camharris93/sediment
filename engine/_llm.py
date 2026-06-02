"""Minimal Anthropic wrapper shared by the AI edge layers (scaffold, orchestrate,
query). Centralizes the client, key resolution, prompt-caching, and usage capture
so every edge layer routes through one place — and so the deterministic core never
imports anything that can talk to an LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .config import get_ai_settings, resolve_anthropic_key


@dataclass
class LLMUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }


def usage_from_response(model: str, msg: Any) -> LLMUsage:
    u = msg.usage
    return LLMUsage(
        model=model,
        input_tokens=int(getattr(u, "input_tokens", 0) or 0),
        output_tokens=int(getattr(u, "output_tokens", 0) or 0),
        cache_creation_input_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        cache_read_input_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
    )


@lru_cache(maxsize=1)
def get_client():
    from anthropic import Anthropic  # imported lazily — core never needs it
    return Anthropic(api_key=resolve_anthropic_key())


def complete(
    *,
    system: str,
    user: str,
    model: str | None = None,
    max_tokens: int = 4096,
    cache_system: bool = True,
) -> tuple[str, LLMUsage]:
    """One-shot completion. Returns (text, usage). Caches the system prompt by
    default (it's the big, stable part — schema/profile context)."""
    s = get_ai_settings()
    model = model or s.model
    client = get_client()
    system_block = [{"type": "text", "text": system}]
    if cache_system:
        system_block[0]["cache_control"] = {"type": "ephemeral"}
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_block,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(getattr(b, "text", "") or "" for b in msg.content)
    return text, usage_from_response(model, msg)
