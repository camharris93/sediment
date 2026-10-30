"""Baselines the harness must beat (docs/investigate.md §9.1).

B1 — today's sediment: `run_multi_hop(question, ctx)`. Prose output, so it is
     scored by a human only.
B2 — the fair "one smart prompt": ONE `complete()` call with the schema and the
     question returns findings with claims, labels and SQL. Each SQL runs through
     L3-L5 (the replay path), so its numbers are real; its LABELS ARE THE MODEL'S
     OWN (a finding whose SQL fails is untestable — it cannot show its number).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .._llm import usage_meter
from ..query.grounding import GroundingContext, to_prompt_summary
from .budget import Budget, BudgetTracker
from .cuts import run_cut_sql, safe_rows
from .numfmt import fmt
from .plan import strip_json
from .runner import RUNS_DIR, make_run_id, write_run
from .types import (
    DIRECTIONS,
    LABELS,
    METRICS,
    Claim,
    Cut,
    EvidenceCell,
    Finding,
    Hypothesis,
    Investigation,
    TestSpec,
    Verdict,
)

B2_SYSTEM = """\
You are a senior data analyst. Investigate the user's question against the
DuckDB warehouse described below and report your findings.

For each finding give: a one-sentence statement, a structured claim, your own
confidence label, and ONE read-only DuckDB SQL query (fully qualified
schema.table names) whose result supports it. Use 2-6 findings. Include findings
that rule explanations out, labelled "refuted".

claim fields: metric (one of {metrics}); dimensions (real column names, or
"derived:<label>" for a bucket you compute); segment ({{column: value}} the claim
is about); direction ("higher" | "lower" | "no_difference"); window
("YYYY-MM..YYYY-MM" or null).
label: one of confirmed | suggestive | refuted | untestable.

Return ONLY JSON:
{{"findings": [{{"statement": "...", "claim": {{...}}, "label": "...", "sql": "..."}}]}}

# Schema
{schema}
"""


def b2_model() -> str:
    from ..config import get_ai_settings
    return os.environ.get("ANTHROPIC_MODEL_STRONG") or get_ai_settings().model


def run_b2(question: str, dataset: str, ctx: GroundingContext, *, out_dir: Path | None = RUNS_DIR,
           model: str | None = None) -> Investigation:
    from .._llm import complete

    inv = Investigation(make_run_id(question, "B2"), question, dataset, "B2", mode="baseline")
    with usage_meter() as meter:
        tracker = BudgetTracker(Budget(), meter)
        try:
            text, _ = complete(system=B2_SYSTEM.format(metrics=", ".join(METRICS), schema=to_prompt_summary(ctx)),
                               user=f"Question: {question}", model=model or b2_model(), max_tokens=12000)
            payload = json.loads(strip_json(text), strict=False)
            raw = payload.get("findings") or []
        except Exception as exc:
            inv.error, raw = f"B2 call failed: {type(exc).__name__}: {exc}", []
        for i, f in enumerate(raw, 1):
            hid, cid = f"h{i}", f"h{i}.c1"
            c = f.get("claim") or {}
            claim = Claim(str(c.get("metric", "other")) if c.get("metric") in METRICS else "other",
                          [str(d) for d in c.get("dimensions") or []], dict(c.get("segment") or {}),
                          c.get("direction") if c.get("direction") in DIRECTIONS else "higher", c.get("window"))
            sql = str(f.get("sql") or "")
            cut = Cut(cid, hid, "test", "(model-written SQL)", [], "-", TestSpec("threshold", "-"), sql=sql)
            inv.hypotheses.append(Hypothesis(hid, str(f.get("statement", "")), "", claim, [cut]))
            r = run_cut_sql(cid, sql, ctx) if sql else None
            tracker.note_query()
            label = f.get("label") if f.get("label") in LABELS else "suggestive"
            cells = []
            if r is not None:
                inv.cut_results.append(r)
                for ri, row in enumerate(safe_rows(r.rows[:10])):
                    for k, v in row.items():
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            cells.append(EvidenceCell(cid, k, {"row": ri}, v))
            if r is None or r.status != "executed":
                label = "untestable"
            inv.verdicts.append(Verdict(cid, label, "model-assigned label (B2)", cells))
            inv.findings.append(Finding(f"F{i}", hid, label, str(f.get("statement", "")), claim, cells,
                                        [sql] if sql else [], judged_label=label,
                                        note=(r.error if r else "no SQL") if label == "untestable" else None))
        inv.usage = tracker.summary()
    if out_dir is not None:
        write_run(inv, out_dir, [])
    return inv


def run_b1(question: str, dataset: str, ctx: GroundingContext, *, out_dir: Path | None = RUNS_DIR) -> Investigation:
    """Today's multi-hop. Stored as an Investigation with no findings and the prose
    in the report — B1 is rated by a human, not auto-scored."""
    from ..query.multihop import run_multi_hop

    inv = Investigation(make_run_id(question, "B1"), question, dataset, "B1", mode="baseline")
    with usage_meter() as meter:
        tracker = BudgetTracker(Budget(), meter)
        try:
            mh = run_multi_hop(question, ctx)
            prose = mh.explanation
            sqls = [h.result.sql for h in mh.hops if h.result and h.result.sql]
            rows = safe_rows(mh.rows[:20]) if mh.rows else []
        except Exception as exc:
            prose, sqls, rows = "", [], []
            inv.error = f"B1 failed: {type(exc).__name__}: {exc}"
        inv.usage = tracker.summary()
    if out_dir is not None:
        d = write_run(inv, out_dir, [])
        lines = [f"# B1 (multi-hop) answer: {question}", "", prose or "(no answer)", ""]
        if rows:
            keys = list(rows[0])
            lines += ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
            lines += ["| " + " | ".join(fmt(r[k]) for k in keys) + " |" for r in rows]
        for s in sqls:
            lines += ["", "```sql", s, "```"]
        (d / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return inv
