"""Conversation state — a session is an ordered series of turns.

Each turn is one user question + the run that answered it. Subsequent turns can
reference a prior turn's result as a synthetic table named `turn_<N>_result` —
the same mechanism multi-hop uses for cross-hop refs (inlined as a CTE at
execution). That's what makes follow-ups work: "now just the mammals", "break
that down by class", "compare to the birds".

In-memory only; sessions live for the process. Persistence is a separate call.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .grounding import (
    ColumnInfo,
    GroundingContext,
    TableInfo,
    build_grounding_context,
    inline_synthetic_ctes,
)
from .orchestrator import PipelineResult
from .trace import TraceEvent

EventCallback = Callable[[TraceEvent], None]


def _infer_type(values: list[Any]) -> str:
    has_int = has_float = has_str = has_bool = has_other = False
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            has_bool = True
        elif isinstance(v, int):
            has_int = True
        elif isinstance(v, float):
            has_float = True
        elif isinstance(v, str):
            has_str = True
        else:
            has_other = True
    if has_float and not has_str and not has_other:
        return "DOUBLE"
    if has_int and not has_str and not has_float and not has_other:
        return "BIGINT"
    if has_bool and not (has_str or has_int or has_float):
        return "BOOLEAN"
    return "VARCHAR"


def synthetic_from_result(name: str, result: PipelineResult, ctx: GroundingContext) -> TableInfo | None:
    """Build a synthetic TableInfo from an executed result. The stored SQL is the
    EXECUTABLE form (prior synthetics inlined) so it's self-contained for reuse."""
    rows = result.rows or []
    sql = result.sql
    if not rows or not sql:
        return None
    columns: list[ColumnInfo] = []
    for col in rows[0].keys():
        values = [r.get(col) for r in rows]
        non_null = [v for v in values if v is not None]
        columns.append(ColumnInfo(
            name=col, type=_infer_type(values),
            nullable=any(v is None for v in values),
            sample_values=[v for v in non_null[:5]],
        ))
    return TableInfo(
        schema="", name=name, fully_qualified=name,
        row_count=result.provenance.row_count if result.provenance else len(rows),
        columns=columns, is_synthetic=True,
        synthetic_sql=inline_synthetic_ctes(sql, ctx),
    )


@dataclass
class Turn:
    index: int                       # 1-based
    question: str
    result: Any                      # MultiHopResult (kept loose to avoid an import cycle)
    synthetic: TableInfo | None = None


@dataclass
class Session:
    dataset: str
    turns: list[Turn] = field(default_factory=list)
    _base_ctx: GroundingContext | None = None

    def base_ctx(self) -> GroundingContext:
        if self._base_ctx is None:
            self._base_ctx = build_grounding_context(self.dataset)
        return self._base_ctx

    def refresh(self) -> None:
        """Drop the cached grounding (after a new model is built for this dataset)."""
        self._base_ctx = None

    def prior_synthetics(self) -> list[TableInfo]:
        return [t.synthetic for t in self.turns if t.synthetic is not None]

    def history_for_prompt(self) -> list[dict[str, Any]]:
        """Compact per-turn summaries the intent/planner layers embed so they can
        resolve references like 'those', 'now just X', 'that ranking'."""
        out: list[dict[str, Any]] = []
        for t in self.turns:
            if t.synthetic is None:
                continue
            sample = t.result.rows[:3] if t.result.rows else []
            out.append({
                "turn": t.index,
                "question": t.question,
                "summary": (t.result.explanation or "")[:200],
                "result_table": t.synthetic.name,
                "columns": [c.name for c in t.synthetic.columns],
                "sample_rows": sample,
                "row_count": t.synthetic.row_count,
            })
        return out


def render_history(history: list[dict[str, Any]]) -> str:
    """Format conversation history for a prompt section."""
    if not history:
        return ""
    lines = ["──────── Conversation so far ────────",
             "Earlier turns in this conversation. Each turn's result is queryable by its "
             "bare name (the `result_table`). Reference it when the user builds on a prior "
             "answer ('those', 'now just X', 'break that down', 'compare to ...')."]
    for t in history:
        lines.append("")
        lines.append(f"Turn {t['turn']}: \"{t['question']}\"")
        lines.append(f"  -> {t['result_table']}  columns={t['columns']}  ({t['row_count']} rows)")
        for r in t["sample_rows"]:
            lines.append(f"     sample: {r}")
    return "\n".join(lines)


def run_turn(session: Session, question: str, *, on_event: EventCallback | None = None):
    """Run one conversational turn (multi-hop). Prior turns' results are visible as
    synthetic tables; the question is planned, run, and synthesized. Returns the
    MultiHopResult. The display hop's result is registered as `turn_<N>_result`."""
    from dataclasses import replace

    from .multihop import run_multi_hop

    idx = len(session.turns) + 1
    mh = run_multi_hop(question, session.base_ctx(), on_event=on_event,
                       history=session.history_for_prompt(),
                       prior_synthetics=session.prior_synthetics())

    # The display hop's synthetic is already self-contained (its prior-hop refs
    # were inlined when it was built); just rename it to the turn-level name.
    syn = None
    disp = mh.display
    if disp is not None and disp.synthetic is not None:
        syn = replace(disp.synthetic, name=f"turn_{idx}_result", fully_qualified=f"turn_{idx}_result")
    session.turns.append(Turn(index=idx, question=question, result=mh, synthetic=syn))
    return mh
