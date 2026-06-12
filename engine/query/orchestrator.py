"""Orchestrator — runs L1->L7, emits a trace, owns the self-correction loop.

The L3 (static) and L4 (dry-run) retry budget is SHARED: both indicate a SQL the
generator must fix, and both feed structured violations back to L2. L5 refusal is
terminal. Mirrors the sql-engine control flow, minus multi-hop (single-hop here).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .dry_run import DryRunReport, dry_run_check
from .execution import ExecutionOutcome, Provenance, Refusal, guarded_execute
from .generation import generate_sql
from .grounding import GroundingContext, inline_synthetic_ctes
from .intent import Intent, derive_intent
from .plausibility import PlausibilityResult, check_plausibility
from .static_validation import ValidationResult, Violation, ViolationKind, validate_sql
from .trace import LayerId, TraceEvent
from .translation import FinalAnswer, translate

MAX_RETRIES = 3


@dataclass
class PipelineResult:
    status: str                     # executed | refused | transparent_failure
    question: str
    intent: Intent | None = None
    sql: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    provenance: Provenance | None = None
    refusal: Refusal | None = None
    plausibility: PlausibilityResult | None = None
    final_answer: FinalAnswer | None = None
    attempts: int = 0
    events: list[TraceEvent] = field(default_factory=list)


EventCallback = Callable[[TraceEvent], None]


def _emit(events: list[TraceEvent], on_event, kind, layer, **payload) -> None:
    ev = TraceEvent(kind=kind, layer=layer, payload=payload)
    events.append(ev)
    if on_event:
        try:
            on_event(ev)
        except Exception:
            pass


@dataclass
class _GenLoop:
    success: bool
    sql: str | None = None
    validation: ValidationResult | None = None
    dry_run: DryRunReport | None = None
    attempts: int = 0


def _generate_validated(intent: Intent, ctx: GroundingContext, events, on_event, max_retries: int) -> _GenLoop:
    sql = validation = dry_run_report = None
    for i in range(max_retries + 1):
        attempt = i + 1
        _emit(events, on_event, "layer_start", LayerId.L2, attempt=attempt)
        try:
            sql, _ = generate_sql(intent, ctx,
                                  prior_sql=sql if i > 0 else None,
                                  prior_validation=validation if i > 0 else None)
        except Exception as exc:
            _emit(events, on_event, "validation_fail", LayerId.L2, error=str(exc), attempt=attempt)
            return _GenLoop(False, sql, validation, dry_run_report, attempt)
        _emit(events, on_event, "layer_result", LayerId.L2, sql=sql, attempt=attempt)

        _emit(events, on_event, "layer_start", LayerId.L3, attempt=attempt)
        validation = validate_sql(sql, ctx)
        if not validation.ok:
            _emit(events, on_event, "validation_fail", LayerId.L3, attempt=attempt,
                  violations=[v.__dict__ for v in validation.violations])
            if i < max_retries:
                _emit(events, on_event, "retry", LayerId.L3, next_attempt=attempt + 1)
                continue
            return _GenLoop(False, sql, validation, dry_run_report, attempt)
        _emit(events, on_event, "layer_result", LayerId.L3, **{"pass": True, "attempt": attempt})

        # Inline any synthetic prior-hop/turn tables as CTEs before EXPLAIN. The
        # user-facing `sql` keeps its readable bare-name refs; this is what runs.
        executable = inline_synthetic_ctes(sql, ctx)
        _emit(events, on_event, "layer_start", LayerId.L4, attempt=attempt)
        dry_run_report = dry_run_check(executable)
        if not dry_run_report.ok:
            _emit(events, on_event, "validation_fail", LayerId.L4, attempt=attempt,
                  error_summary=dry_run_report.summary)
            validation = ValidationResult(False, [Violation(
                ViolationKind.parse_error,
                f"DuckDB EXPLAIN rejected the query: {dry_run_report.summary}")])
            if i < max_retries:
                _emit(events, on_event, "retry", LayerId.L4, next_attempt=attempt + 1)
                continue
            return _GenLoop(False, sql, validation, dry_run_report, attempt)
        _emit(events, on_event, "layer_result", LayerId.L4, **{"pass": True, "attempt": attempt})
        return _GenLoop(True, sql, validation, dry_run_report, attempt)
    return _GenLoop(False, sql, validation, dry_run_report, max_retries + 1)


def run_to_executed_answer(question: str, ctx: GroundingContext, *,
                           max_retries: int = MAX_RETRIES,
                           on_event: EventCallback | None = None,
                           history: list[dict] | None = None) -> PipelineResult:
    events: list[TraceEvent] = []

    _emit(events, on_event, "layer_start", LayerId.L1)
    try:
        intent, _ = derive_intent(question, ctx, history=history)
    except Exception as exc:
        _emit(events, on_event, "validation_fail", LayerId.L1, error=str(exc))
        return PipelineResult("transparent_failure", question, events=events)
    _emit(events, on_event, "layer_result", LayerId.L1, intent=intent.to_dict())

    loop = _generate_validated(intent, ctx, events, on_event, max_retries)
    if not loop.success:
        _emit(events, on_event, "final_answer", None, status="transparent_failure", attempts=loop.attempts)
        final = translate(question=question, status="transparent_failure", intent=intent,
                          sql=loop.sql, provenance=None, plausibility=None, refusal=None,
                          rows=[], events=events)
        return PipelineResult("transparent_failure", question, intent=intent, sql=loop.sql,
                              final_answer=final, attempts=loop.attempts, events=events)

    _emit(events, on_event, "layer_start", LayerId.L5)
    executable_for_l5 = inline_synthetic_ctes(loop.sql or "", ctx)
    outcome: ExecutionOutcome = guarded_execute(executable_for_l5, loop.dry_run or DryRunReport(ok=False))
    if not outcome.ok:
        _emit(events, on_event, "validation_fail", LayerId.L5,
              refusal=outcome.refusal.to_dict() if outcome.refusal else {})
        final = translate(question=question, status="refused", intent=intent, sql=loop.sql,
                          provenance=None, plausibility=None, refusal=outcome.refusal,
                          rows=[], events=events)
        return PipelineResult("refused", question, intent=intent, sql=loop.sql,
                              refusal=outcome.refusal, final_answer=final,
                              attempts=loop.attempts, events=events)
    _emit(events, on_event, "layer_result", LayerId.L5, **{
        "pass": True,
        "row_count": outcome.provenance.row_count if outcome.provenance else 0,
        "elapsed_ms": outcome.provenance.elapsed_ms if outcome.provenance else 0,
        "truncated": outcome.provenance.truncated if outcome.provenance else False})

    _emit(events, on_event, "layer_start", LayerId.L6)
    plausibility = check_plausibility(loop.sql or "", outcome.rows, ctx)
    if plausibility.ok:
        _emit(events, on_event, "layer_result", LayerId.L6, **{"pass": True})
    else:
        _emit(events, on_event, "validation_fail", LayerId.L6,
              warnings=[{"kind": w.kind.value, "message": w.message} for w in plausibility.warnings])

    _emit(events, on_event, "layer_start", LayerId.L7)
    final = translate(question=question, status="executed", intent=intent, sql=loop.sql,
                      provenance=outcome.provenance, plausibility=plausibility, refusal=None,
                      rows=outcome.rows, events=events)
    _emit(events, on_event, "layer_result", LayerId.L7, trust_badge=final.trust_badge.value)
    _emit(events, on_event, "final_answer", None, status="executed", trust_badge=final.trust_badge.value)

    return PipelineResult("executed", question, intent=intent, sql=loop.sql, rows=outcome.rows,
                          provenance=outcome.provenance, plausibility=plausibility,
                          final_answer=final, attempts=loop.attempts, events=events)
