"""L7 — Translation & provenance. Produces a plain-English explanation and a
TRUST BADGE derived from the real layer outcomes. Principle: every number in the
explanation must trace to the visible, executed SQL — the model summarizes
computed values, it does not narrate freely.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .execution import Provenance, Refusal
from .intent import Intent
from .plausibility import PlausibilityResult, PlausibilityWarning
from .trace import TraceEvent


class TrustBadge(str, Enum):
    clean = "clean"
    self_corrected = "self_corrected"
    flagged = "flagged"
    refused = "refused"
    failed = "failed"


@dataclass
class FinalAnswer:
    explanation: str
    sql: str | None
    assumptions: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    trust_badge: TrustBadge = TrustBadge.clean
    plausibility_warnings: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    refusal: dict[str, Any] | None = None


def _badge(status: str, events: list[TraceEvent], plausibility: PlausibilityResult | None,
           refusal: Refusal | None) -> TrustBadge:
    if status == "transparent_failure":
        return TrustBadge.failed
    if status == "refused" or refusal is not None:
        return TrustBadge.refused
    if plausibility is not None and not plausibility.ok:
        return TrustBadge.flagged
    had_retry = any(ev.kind == "retry" for ev in events)
    return TrustBadge.self_corrected if had_retry else TrustBadge.clean


_SYSTEM = """\
You are the TRANSLATION layer (L7) of a trust-first NL->SQL agent. You are NOT
writing SQL — you explain a result that has already been computed and validated.
Return 2-4 plain-English sentences. Hard rules:
  1. Every numeric claim MUST be a value present in the result rows. Invent nothing.
  2. If plausibility warnings are present, acknowledge them.
  3. Surface any assumptions that materially change interpretation.
  4. No markdown.
"""


def _format_input(question: str, intent: Intent | None, sql: str | None,
                  sample: list[dict], warnings: list[PlausibilityWarning]) -> str:
    lines = [f"User question: {question}"]
    if intent and intent.assumptions:
        lines.append("Assumptions:")
        lines += [f"  - {a}" for a in intent.assumptions]
    if sql:
        lines.append("\nExecuted SQL:\n" + sql)
    lines.append("\nResult sample:")
    if not sample:
        lines.append("  (no rows)")
    else:
        keys = list(sample[0].keys())
        lines.append("  | " + " | ".join(keys) + " |")
        for r in sample[:10]:
            lines.append("  | " + " | ".join(str(r[k]) for k in keys) + " |")
    if warnings:
        lines.append("\nPlausibility warnings (acknowledge these):")
        lines += [f"  - [{w.kind.value}] {w.message}" for w in warnings]
    return "\n".join(lines)


def translate(*, question: str, status: str, intent: Intent | None, sql: str | None,
              provenance: Provenance | None, plausibility: PlausibilityResult | None,
              refusal: Refusal | None, rows: list[dict[str, Any]], events: list[TraceEvent]) -> FinalAnswer:
    badge = _badge(status, events, plausibility, refusal)
    sample = rows[:10]

    if status == "transparent_failure":
        explanation = (
            "The agent declined to answer: the generator could not produce SQL that passed the "
            "static and dry-run validators within the retry budget. Rather than return a "
            "confident-but-unvalidated guess, this is a transparent failure.")
    elif status == "refused":
        explanation = (
            f"The agent refused to execute the query: {refusal.message if refusal else 'refused'}.")
    elif intent is None:
        explanation = "Result produced, but no intent was available to summarize."
    else:
        try:
            from .._llm import complete
            from ..config import get_ai_settings
            text, _ = complete(
                system=_SYSTEM,
                user=_format_input(question, intent, sql, sample,
                                   plausibility.warnings if plausibility else []),
                model=get_ai_settings().model_l7, max_tokens=512, cache_system=False)
            explanation = text.strip()
        except Exception as exc:
            explanation = (f"Result produced ({len(rows)} rows). Explanation generation failed: {exc}. "
                           "The SQL is shown and all numbers trace to it.")

    return FinalAnswer(
        explanation=explanation, sql=sql,
        assumptions=(intent.assumptions if intent else []),
        elapsed_ms=provenance.elapsed_ms if provenance else 0,
        trust_badge=badge,
        plausibility_warnings=[{"kind": w.kind.value, "message": w.message, "detail": w.detail}
                               for w in (plausibility.warnings if plausibility else [])],
        row_count=provenance.row_count if provenance else len(rows),
        sample_rows=sample,
        refusal=refusal.to_dict() if refusal else None,
    )
