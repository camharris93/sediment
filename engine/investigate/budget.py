"""Budget accounting for one investigation (docs/investigate.md §5.7).

Checked BETWEEN cuts: when exhausted, the remaining cuts are marked untestable
with reason `budget_*` and the run finishes and reports normally. One cut can
overshoot by its own worst case (about 1 L1 + 4 L2 calls).
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from .._llm import UsageMeter

# USD per million tokens: (input, output). Cache writes bill at 1.25x input and
# cache reads at 0.1x input. Verify against current Anthropic pricing before
# trusting cost numbers; an unknown model falls back to DEFAULT_PRICE and the
# run's usage records that it did.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
DEFAULT_PRICE = (3.0, 15.0)


def price_of(model: str) -> tuple[tuple[float, float], bool]:
    for k, p in PRICES.items():
        if model.startswith(k):
            return p, True
    return DEFAULT_PRICE, False


def usd(meter: UsageMeter) -> tuple[float, list[str]]:
    total, unpriced = 0.0, []
    for model, u in meter.by_model.items():
        (pin, pout), known = price_of(model)
        if not known:
            unpriced.append(model)
        total += (u.input_tokens * pin + u.cache_creation_input_tokens * pin * 1.25
                  + u.cache_read_input_tokens * pin * 0.1 + u.output_tokens * pout) / 1e6
    return round(total, 4), unpriced


@dataclass
class Budget:
    max_hypotheses: int = 4
    max_cuts: int = 12
    max_skeptic_cuts: int = 6
    max_queries: int = 18
    max_input_tokens: int = 250_000
    max_output_tokens: int = 40_000
    max_usd: float = 1.00
    max_wall_s: float = 600

    def to_dict(self) -> dict:
        return asdict(self)


class BudgetTracker:
    def __init__(self, budget: Budget, meter: UsageMeter):
        self.budget = budget
        self.meter = meter
        self.queries = 0
        self.started = time.monotonic()

    def note_query(self) -> None:
        self.queries += 1

    @property
    def wall_s(self) -> float:
        return time.monotonic() - self.started

    def exhausted(self) -> str | None:
        """The first exhausted budget dimension, or None."""
        b = self.budget
        if self.queries >= b.max_queries:
            return "budget_queries"
        if self.meter.input_tokens >= b.max_input_tokens or self.meter.output_tokens >= b.max_output_tokens:
            return "budget_tokens"
        if usd(self.meter)[0] >= b.max_usd:
            return "budget_usd"
        if self.wall_s >= b.max_wall_s:
            return "wall_clock"
        return None

    def summary(self) -> dict:
        cost, unpriced = usd(self.meter)
        return {**self.meter.to_dict(), "usd": cost, "unpriced_models": unpriced,
                "queries": self.queries, "wall_ms": int(self.wall_s * 1000)}
