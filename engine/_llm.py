"""Minimal Anthropic wrapper shared by the AI edge layers (scaffold, orchestrate,
query). Centralizes the client, key resolution, prompt-caching, and usage capture
so every edge layer routes through one place — and so the deterministic core never
imports anything that can talk to an LLM.
"""
from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
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


@dataclass
class UsageMeter:
    """Accumulates every `complete()` call made while it is active. Per-model
    totals so a budget can price them; `calls` counts LLM round-trips."""
    calls: int = 0
    by_model: dict[str, LLMUsage] = field(default_factory=dict)

    def add(self, u: LLMUsage) -> None:
        self.calls += 1
        acc = self.by_model.setdefault(u.model, LLMUsage(model=u.model))
        acc.input_tokens += u.input_tokens
        acc.output_tokens += u.output_tokens
        acc.cache_creation_input_tokens += u.cache_creation_input_tokens
        acc.cache_read_input_tokens += u.cache_read_input_tokens

    @property
    def input_tokens(self) -> int:
        return sum(u.input_tokens + u.cache_creation_input_tokens + u.cache_read_input_tokens
                   for u in self.by_model.values())

    @property
    def output_tokens(self) -> int:
        return sum(u.output_tokens for u in self.by_model.values())

    def to_dict(self) -> dict[str, Any]:
        return {"calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "by_model": {m: u.to_dict() for m, u in self.by_model.items()}}


_METER: contextvars.ContextVar[UsageMeter | None] = contextvars.ContextVar("sediment_usage_meter",
                                                                           default=None)


@contextmanager
def usage_meter() -> Iterator[UsageMeter]:
    """`with usage_meter() as m:` — every `complete()` inside adds to `m`. Nested
    meters each see only their own block's calls."""
    m = UsageMeter()
    token = _METER.set(m)
    try:
        yield m
    finally:
        _METER.reset(token)


def record_usage(u: LLMUsage) -> None:
    """Add a usage to the active meter (no-op when none is active)."""
    m = _METER.get()
    if m is not None:
        m.add(u)


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
    usage = usage_from_response(model, msg)
    record_usage(usage)
    return text, usage
