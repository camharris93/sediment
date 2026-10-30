"""The cut runner (docs/investigate.md §5.2).

`run_cut` sends a cut's natural-language question through the WHOLE existing
L1->L7 pipeline (`run_to_executed_answer`, explain=False) — no bypass. `run_cut_sql`
is the replay path: known SQL through the deterministic L3->L6 layers only, used
by `--replay`, hand-authored reference plans, and the B2 baseline.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .._llm import usage_meter
from ..query.grounding import GroundingContext
from ..query.trace import TraceEvent
from .types import Cut, CutResult

EventCallback = Callable[[TraceEvent], None]


def json_safe(v: Any) -> Any:
    """Rows are stored as JSON, so normalize once at the source: a live run and
    its replay then judge byte-identical values."""
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def safe_rows(rows: list[dict]) -> list[dict]:
    return [{k: json_safe(v) for k, v in r.items()} for r in rows]


def definitions_text(definitions: list[dict]) -> str:
    return "; ".join(f"{d.get('id', 'def')}: {d['text']}" for d in definitions if d.get("text"))


def pipeline_question(cut: Cut, definitions: list[dict]) -> str:
    """The NL the pipeline sees. Definitions travel in the question text because
    the orchestrator has no definitions slot (a known limitation, §11)."""
    q = cut.question.strip()
    defs = definitions_text(definitions)
    if defs:
        q += f"\nDefinitions in force: {defs}"
    q += (f"\nReturn exactly these columns: {', '.join(cut.expected_columns)}. "
          f"One row per {cut.grain}. Aggregate in SQL; do not return raw rows.")
    return q


def _tagger(cut_id: str, on_event: EventCallback | None) -> EventCallback | None:
    if on_event is None:
        return None

    def tagged(ev: TraceEvent) -> None:
        ev.payload = {**ev.payload, "cut_id": cut_id}
        on_event(ev)
    return tagged


def _events_json(events: list[TraceEvent], cut_id: str) -> list[dict]:
    return [{"cut_id": cut_id, "kind": e.kind, "layer": e.layer.value if e.layer else None,
             "payload": {k: json_safe(v) if not isinstance(v, (list, dict)) else v
                         for k, v in e.payload.items()}} for e in events]


def run_cut(cut: Cut, ctx: GroundingContext, definitions: list[dict] | None = None, *,
            on_event: EventCallback | None = None, trace: list[dict] | None = None) -> CutResult:
    from ..query.orchestrator import run_to_executed_answer

    question = pipeline_question(cut, definitions or [])
    t0 = time.monotonic()
    with usage_meter() as m:
        try:
            res = run_to_executed_answer(question, ctx, on_event=_tagger(cut.id, on_event), explain=False)
        except Exception as exc:  # never let one cut kill the investigation
            return CutResult(cut.id, "transparent_failure", None, [], "failed",
                             elapsed_ms=int((time.monotonic() - t0) * 1000), usage=m.to_dict(),
                             error=f"{type(exc).__name__}: {exc}")
    if trace is not None:
        trace.extend(_events_json(res.events, cut.id))
    fa = res.final_answer
    return CutResult(
        cut_id=cut.id, status=res.status, sql=res.sql, rows=safe_rows(res.rows),
        trust_badge=fa.trust_badge.value if fa else "failed",
        warnings=fa.plausibility_warnings if fa else [],
        assumptions=list(res.intent.assumptions) if res.intent else [],
        truncated=bool(res.provenance.truncated) if res.provenance else False,
        attempts=res.attempts, elapsed_ms=int((time.monotonic() - t0) * 1000), usage=m.to_dict(),
        error=(res.refusal.message if res.refusal else None))


def run_cut_sql(cut_id: str, sql: str, ctx: GroundingContext) -> CutResult:
    """Known SQL through L3 (static) -> L4 (dry run) -> L5 (guarded execute) -> L6
    (plausibility). Same badge rules as the live path, minus retries."""
    from ..query.dry_run import dry_run_check
    from ..query.execution import guarded_execute
    from ..query.grounding import inline_synthetic_ctes
    from ..query.plausibility import check_plausibility
    from ..query.static_validation import validate_sql

    t0 = time.monotonic()

    def done(status, badge, *, rows=(), warnings=(), truncated=False, error=None):
        return CutResult(cut_id, status, sql, safe_rows(list(rows)), badge, list(warnings), [],
                         truncated, 1, int((time.monotonic() - t0) * 1000), {}, error)

    v = validate_sql(sql, ctx)
    if not v.ok:
        return done("transparent_failure", "failed",
                    error="L3: " + "; ".join(x.to_feedback_line() for x in v.violations))
    executable = inline_synthetic_ctes(sql, ctx)
    dr = dry_run_check(executable)
    if not dr.ok:
        return done("transparent_failure", "failed", error=f"L4: {dr.summary}")
    outcome = guarded_execute(executable, dr)
    if not outcome.ok:
        return done("refused", "refused", error=outcome.refusal.message if outcome.refusal else "refused")
    pl = check_plausibility(sql, outcome.rows, ctx)
    warnings = [{"kind": w.kind.value, "message": w.message, "detail": w.detail} for w in pl.warnings]
    return done("executed", "clean" if pl.ok else "flagged", rows=outcome.rows, warnings=warnings,
                truncated=bool(outcome.provenance.truncated) if outcome.provenance else False)
