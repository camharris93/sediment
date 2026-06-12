"""Multi-hop analysis — plan → run hops → synthesize.

For a complex question, the PLANNER decomposes it into an ordered list of hops
(a single-hop plan is fine). Each hop runs the full L1-L7 pipeline; its result is
registered as a `hop_<N>_result` synthetic table that later hops can query (the
same CTE-inlining mechanism conversation uses). The SYNTHESIZER then ties the
hops together into one answer and picks which hop's result is the headline.

Ported from the sibling sql-engine; adapted to DuckDB. The mid-plan revisor from
the original is left out of v1 (the plan executes as proposed).
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .conversation import render_history, synthetic_from_result
from .grounding import GroundingContext, TableInfo, to_prompt_summary
from .orchestrator import PipelineResult, run_to_executed_answer
from .trace import TraceEvent
from .translation import TrustBadge

EventCallback = Callable[[TraceEvent], None]
MAX_HOPS = 5


# ─────────────────────────────────────────────────────────────────────────────
# Data shapes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Hop:
    description: str
    rationale: str = ""
    inputs: list[str] = field(default_factory=list)
    output_name: str = ""
    expected_output: str = ""


@dataclass
class Plan:
    hops: list[Hop]
    rationale: str = ""


@dataclass
class HopOutcome:
    hop_index: int
    hop: Hop
    result: PipelineResult
    synthetic: TableInfo | None
    success: bool


@dataclass
class MultiHopResult:
    question: str
    plan: Plan | None
    hops: list[HopOutcome]
    explanation: str
    display_hop_index: int
    trust_badge: TrustBadge
    events: list[TraceEvent] = field(default_factory=list)
    status: str = "ok"

    @property
    def display(self) -> HopOutcome | None:
        return next((h for h in self.hops if h.hop_index == self.display_hop_index),
                    self.hops[-1] if self.hops else None)

    @property
    def sql(self) -> str | None:
        d = self.display
        return d.result.sql if d else None

    @property
    def rows(self) -> list[dict[str, Any]]:
        d = self.display
        return d.result.rows if d else []

    @property
    def attempts(self) -> int:
        return sum(h.result.attempts for h in self.hops)

    @property
    def is_multi(self) -> bool:
        return len(self.hops) > 1


def _emit(events, on_event, kind, **payload) -> None:
    ev = TraceEvent(kind=kind, layer=None, payload=payload)
    events.append(ev)
    if on_event:
        try:
            on_event(ev)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Planner
# ─────────────────────────────────────────────────────────────────────────────

_PLANNER_SYSTEM = """\
You are the PLANNER of a multi-hop analytical SQL agent. Decide whether to answer
the question in ONE hop or break it into MULTIPLE hops, then emit an ordered plan.

A "hop" is one full pass through generate→validate→run. Each hop's result becomes
a table named `hop_<N>_result` that later hops can query by bare name.

Use ONE hop when the question maps to a single SQL query (even a complex one with
CTEs/windows/joins), as long as you don't need to INSPECT an intermediate result
before deciding the next step.

Use MULTIPLE hops when the analysis genuinely benefits from inspectable
intermediates: "compute X, then for the top N from X compute Y"; comparisons or
correlations across results derived at different grains; a single-SQL version
would be huge and fragile.

Return a SINGLE JSON object (no markdown):
{
  "hops": [
    {"description": "...", "rationale": "...", "inputs": ["schema.table or hop_1_result"],
     "output_name": "hop_1_result", "expected_output": "one line on the result rows"}
  ],
  "rationale": "<one paragraph: why this decomposition fits>"
}

Rules:
  1. Max 5 hops. Most questions need 1-3. Prefer fewer; use multi-hop only when it
     genuinely helps.
  2. output_name MUST be `hop_<index>_result` (1-based).
  3. A hop's inputs may reference EARLIER hops' output_names, never later ones.
  4. Reference only entities in the grounded schema below (and earlier hop results).
  5. If the question references prior CONVERSATION turns, a hop may read from those
     `turn_<N>_result` tables.

-------- Grounded schema --------
__SCHEMA__

__HISTORY__
"""


def _strip_json(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    m = re.search(r"\{[\s\S]*\}", t)
    return m.group(0) if m else t


def propose_plan(question: str, ctx: GroundingContext, history: list[dict] | None = None) -> Plan:
    from .._llm import complete
    from ..config import get_ai_settings

    system = (_PLANNER_SYSTEM
              .replace("__SCHEMA__", to_prompt_summary(ctx))
              .replace("__HISTORY__", render_history(history or [])))
    text, _ = complete(system=system, user=f"Question: {question}",
                       model=get_ai_settings().model_l1, max_tokens=1500)
    payload = json.loads(_strip_json(text), strict=False)
    hops = [Hop(description=h.get("description", ""), rationale=h.get("rationale", ""),
                inputs=h.get("inputs") or [], output_name=h.get("output_name", ""),
                expected_output=h.get("expected_output", ""))
            for h in (payload.get("hops") or [])]
    # Normalize hop output names to the convention.
    for i, h in enumerate(hops, start=1):
        h.output_name = f"hop_{i}_result"
    if not hops:
        hops = [Hop(description=question, output_name="hop_1_result")]
    return Plan(hops=hops[:MAX_HOPS], rationale=payload.get("rationale", ""))


# ─────────────────────────────────────────────────────────────────────────────
# Synthesizer
# ─────────────────────────────────────────────────────────────────────────────

_SYNTH_SYSTEM = """\
You are the SYNTHESIZER of a multi-hop analytical agent. A sequence of hops just
ran. Produce the final user-facing summary tying them together.

Return a SINGLE JSON object (no markdown):
{
  "explanation": "<3-5 sentences tying the hops together, ending with the actual answer. Every number must appear in the result rows.>",
  "display_hop_index": <which hop's result is the headline answer (usually the last)>
}

If a hop failed or returned nothing, acknowledge it — honesty over polish.
"""


def _format_synth_input(question: str, plan: Plan, hops: list[HopOutcome]) -> str:
    lines = [f"User question: {question}", f"\nPlan rationale: {plan.rationale or '(none)'}", "\nHops:"]
    for h in hops:
        lines.append(f"\n  Hop {h.hop_index}: {h.hop.description}")
        lines.append(f"    status: {h.result.status}")
        rows = h.result.rows or []
        if rows:
            keys = list(rows[0].keys())
            lines.append(f"    columns: {', '.join(keys)} ({len(rows)} rows)")
            for r in rows[:5]:
                lines.append("      " + " | ".join(f"{k}={r.get(k)}" for k in keys))
        else:
            lines.append("    (no rows)")
    return "\n".join(lines)


def _derive_badge(hops: list[HopOutcome]) -> TrustBadge:
    if not hops:
        return TrustBadge.failed
    if any(h.result.status == "refused" for h in hops):
        return TrustBadge.refused
    if any(h.result.status == "transparent_failure" for h in hops):
        return TrustBadge.failed
    if any(h.result.plausibility and not h.result.plausibility.ok for h in hops):
        return TrustBadge.flagged
    if any(h.result.attempts > 1 for h in hops):
        return TrustBadge.self_corrected
    return TrustBadge.clean


def synthesize(question: str, plan: Plan, hops: list[HopOutcome]) -> tuple[str, int, TrustBadge]:
    badge = _derive_badge(hops)
    successful = [h for h in hops if h.success]
    if not successful:
        return ("The analysis did not complete — no hop produced a validated result. "
                "This is a transparent failure rather than a guess.",
                hops[-1].hop_index if hops else 1, badge)

    # Single-hop: reuse that hop's own L7 explanation; no extra call.
    if len(hops) == 1:
        fa = hops[0].result.final_answer
        return (fa.explanation if fa else "", hops[0].hop_index, badge)

    try:
        from .._llm import complete
        from ..config import get_ai_settings
        text, _ = complete(system=_SYNTH_SYSTEM, user=_format_synth_input(question, plan, hops),
                           model=get_ai_settings().model_l7, max_tokens=900, cache_system=False)
        parsed = json.loads(_strip_json(text), strict=False)
        explanation = str(parsed.get("explanation") or "").strip()
        display = int(parsed.get("display_hop_index") or successful[-1].hop_index)
        if not any(h.hop_index == display for h in successful):
            display = successful[-1].hop_index
        return explanation, display, badge
    except Exception as exc:
        return (f"Completed {len(successful)} hops; synthesis failed: {exc}",
                successful[-1].hop_index, badge)


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def _tag(on_event, hop_index):
    def wrapped(ev: TraceEvent) -> None:
        ev.payload = {**ev.payload, "hop_index": hop_index}
        if on_event:
            on_event(ev)
    return wrapped


def run_multi_hop(question: str, ctx: GroundingContext, *,
                  on_event: EventCallback | None = None,
                  history: list[dict] | None = None,
                  prior_synthetics: list[TableInfo] | None = None,
                  max_hops: int = MAX_HOPS) -> MultiHopResult:
    """Plan, run hops, synthesize. `prior_synthetics` are prior-TURN result
    tables (conversation); they're visible to every hop."""
    events: list[TraceEvent] = []
    eff = ctx.with_synthetic_tables(prior_synthetics) if prior_synthetics else ctx

    try:
        plan = propose_plan(question, eff, history)
    except Exception as exc:
        _emit(events, on_event, "plan_failed", error=str(exc))
        return MultiHopResult(question, None, [], f"Could not plan the analysis: {exc}",
                              1, TrustBadge.failed, events, status="plan_failed")
    _emit(events, on_event, "plan_proposed",
          hops=[h.description for h in plan.hops], rationale=plan.rationale)

    outcomes: list[HopOutcome] = []
    hop_synthetics: list[TableInfo] = []
    for idx, hop in enumerate(plan.hops[:max_hops], start=1):
        augmented = eff.with_synthetic_tables(hop_synthetics) if hop_synthetics else eff
        _emit(events, on_event, "hop_start", hop_index=idx, description=hop.description)
        result = run_to_executed_answer(hop.description, augmented, on_event=_tag(on_event, idx))
        success = result.status == "executed"
        syn = synthetic_from_result(f"hop_{idx}_result", result, augmented) if success else None
        if syn:
            hop_synthetics.append(syn)
        _emit(events, on_event, "hop_result", hop_index=idx, status=result.status,
              rows=result.provenance.row_count if result.provenance else 0)
        outcomes.append(HopOutcome(idx, hop, result, syn, success))
        if not success:
            break

    explanation, display_idx, badge = synthesize(question, plan, outcomes)
    _emit(events, on_event, "synthesis", trust_badge=badge.value, display_hop_index=display_idx)
    status = "ok" if any(h.success for h in outcomes) else "no_hops_succeeded"
    return MultiHopResult(question, plan, outcomes, explanation, display_idx, badge, events, status)
