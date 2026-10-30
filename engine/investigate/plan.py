"""The planner (docs/investigate.md §5.1): ONE LLM call proposes competing
hypotheses, each with 1-3 aggregated cuts and a deterministic TestSpec per cut.
Python validates the shape; one repair retry (the L2 `prior_validation` pattern),
then a transparent failure.

The prompt is dataset-agnostic by contract: tests/test_prompt_leak.py greps it
for answer-key vocabulary.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from ..query.grounding import GroundingContext, to_prompt_summary
from .budget import Budget
from .evaluate import iso_period
from .memory import Memory
from .types import DIRECTIONS, METRICS, REST, TEST_KINDS, Claim, Cut, Hypothesis, TestSpec

PLANNER_SYSTEM = """\
You are the PLANNING layer of an analytics investigation harness. You do NOT write
SQL and you do NOT compute or quote numbers. You turn an open analytical question
into COMPETING, TESTABLE hypotheses, and for each one describe the aggregated data
cuts that would test it. A separate NL->SQL pipeline runs each cut; deterministic
Python then judges each cut with the test you specify. Your job is to make every
hypothesis falsifiable by the data in the schema below.

Rules:
 1. Propose {min_h}-{max_h} hypotheses that COMPETE: if one is true, another is less
    likely. Do not propose restatements of the question.
 2. At least one hypothesis must be a "boring" explanation, marked "boring": true:
    a population or mix shift, a calendar/seasonal effect, a small-sample artifact,
    or a definition / data-quality artifact.
 3. Each hypothesis gets 1-3 cuts (at most {max_cuts} cuts in total). A cut is ONE
    aggregated query in plain English: it names the metric, the grouping, the
    filters and the time window, and it returns a SMALL table (well under 200
    rows). Always return the denominator alongside any rate (e.g. the number of
    accounts the rate is computed over) so sample size can be checked.
 4. Name every output column the test needs in "expected_columns" (snake_case
    aliases the SQL must return), and describe the row grain ("one row per X").
 5. Every cut has a "test" from this menu. Column fields must appear in
    expected_columns:
    - group_diff: compare a segment against a comparison group.
        value_col (the rate/average), n_col (denominator per row), group_col (the
        column holding the segment label), a (the segment's value in group_col),
        b (the comparison value, or "__rest__" for all other rows pooled),
        direction ("higher": a's value_col is higher than b's; "lower": lower).
        Several rows per group (e.g. per month) are pooled, weighted by n_col.
    - before_after: compare periods. time_col (a date or 'YYYY-MM' column),
        split_at (ISO date; rows >= split_at are "after"), value_col, n_col,
        direction ("higher": after is higher than before). Optionally group_col +
        a to restrict the comparison to one segment's rows.
    - threshold: a single value against a bound. value_col, n_col, b (the
        numeric bound), direction ("higher": value >= b; "lower": value <= b),
        optionally group_col + a to pick the row.
    - reconcile: part_col summed over rows must equal total_col (data checks).
    Optional: min_rel_diff (default 0.20 relative difference) and min_n
    (default 30 per arm). Do not lower them to make a test easier to pass.
 6. Each hypothesis has a structured "claim":
    - metric: one of {metrics}
    - dimensions: the columns the claim is about — real column names from the
      schema, or "derived:<snake_case_label>" for a bucket you define in a cut
      (e.g. a tenure bucket). List every dimension the claim depends on.
    - segment: {{column_or_derived_label: value}} — the group the claim is about.
    - direction: "higher" | "lower" | "no_difference" — how the segment's metric
      compares with its comparison group or earlier period.
    - window: "YYYY-MM..YYYY-MM" or null.
 7. If memory lists a known finding that answers part of the question, you may
    restate it as a hypothesis with "known_from": "<id>" and still give it cuts —
    memory is context, never evidence.

Return ONLY JSON:
{{"hypotheses": [{{"statement": "...", "mechanism": "...", "boring": false,
   "known_from": null,
   "claim": {{"metric": "...", "dimensions": ["..."], "segment": {{}}, "direction": "higher", "window": null}},
   "cuts": [{{"question": "...", "expected_columns": ["..."], "grain": "one row per ...",
             "test": {{"kind": "group_diff", "value_col": "...", "n_col": "...", "group_col": "...",
                      "a": "...", "b": "...", "direction": "higher"}}}}]}}]}}

# Schema
{schema}
"""


class PlanError(RuntimeError):
    def __init__(self, message: str, errors: list[str] | None = None, raw: str | None = None):
        super().__init__(message)
        self.errors = errors or []
        self.raw = raw


def planner_system(ctx: GroundingContext, budget: Budget, min_h: int = 3) -> str:
    return PLANNER_SYSTEM.format(min_h=min_h, max_h=budget.max_hypotheses, max_cuts=budget.max_cuts,
                                 metrics=", ".join(METRICS), schema=to_prompt_summary(ctx))


def planner_user(question: str, memory: Memory | None, today: str | None = None) -> str:
    parts = [f"Question to investigate: {question}"]
    if today:
        parts.append(f"(Today is {today}.)")
    block = memory.prompt_block() if memory else ""
    if block:
        parts.append(block)
    return "\n\n".join(parts)


def strip_json(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if i >= 0 and j > i else t


def known_columns(ctx: GroundingContext) -> set[str]:
    return {c.name.lower() for t in ctx.tables for c in t.columns}


def _is_dim(d: str, cols: set[str]) -> bool:
    return isinstance(d, str) and (d.lower().startswith("derived:") or d.lower() in cols)


def validate_plan(payload: Any, ctx: GroundingContext, budget: Budget) -> tuple[list[Hypothesis], list[str]]:
    """Deterministic shape check. Returns (hypotheses, errors); ids are assigned
    here (h1, h1.c1, ...), never taken from the model."""
    errs: list[str] = []
    cols = known_columns(ctx)
    if not isinstance(payload, dict) or not isinstance(payload.get("hypotheses"), list):
        return [], ["top level must be an object with a 'hypotheses' list"]
    raw_hs = payload["hypotheses"]
    if not 2 <= len(raw_hs) <= budget.max_hypotheses:
        errs.append(f"need 2-{budget.max_hypotheses} hypotheses, got {len(raw_hs)}")
    if raw_hs and not any(isinstance(h, dict) and h.get("boring") for h in raw_hs):
        errs.append('at least one hypothesis must be marked "boring": true (mix shift, seasonality, '
                    "small sample, or a definition/data artifact)")
    out: list[Hypothesis] = []
    total_cuts = 0
    for hi, h in enumerate(raw_hs[:budget.max_hypotheses], 1):
        hid = f"h{hi}"
        if not isinstance(h, dict):
            errs.append(f"{hid}: not an object")
            continue
        c = h.get("claim") or {}
        claim = Claim(metric=str(c.get("metric", "")), dimensions=list(c.get("dimensions") or []),
                      segment=dict(c.get("segment") or {}), direction=str(c.get("direction", "")),
                      window=c.get("window"))
        if claim.metric not in METRICS:
            errs.append(f"{hid}.claim.metric {claim.metric!r} not in {list(METRICS)}")
        if not claim.dimensions:
            errs.append(f"{hid}.claim.dimensions is empty")
        for d in claim.dimensions:
            if not _is_dim(d, cols):
                errs.append(f"{hid}.claim.dimensions: {d!r} is not a schema column; use 'derived:<label>'")
        for k in claim.segment:
            if not _is_dim(k, cols):
                errs.append(f"{hid}.claim.segment key {k!r} is not a schema column or 'derived:<label>'")
        if claim.direction not in DIRECTIONS:
            errs.append(f"{hid}.claim.direction {claim.direction!r} not in {list(DIRECTIONS)}")
        cuts: list[Cut] = []
        raw_cuts = h.get("cuts") or []
        if not 1 <= len(raw_cuts) <= 3:
            errs.append(f"{hid}: needs 1-3 cuts, got {len(raw_cuts)}")
        for ci, rc in enumerate(raw_cuts[:3], 1):
            cid = f"{hid}.c{ci}"
            total_cuts += 1
            if not isinstance(rc, dict):
                errs.append(f"{cid}: not an object")
                continue
            expected = [str(x) for x in rc.get("expected_columns") or []]
            t = rc.get("test") or {}
            kind = t.get("kind")
            if kind not in TEST_KINDS:
                errs.append(f"{cid}.test.kind {kind!r} not in {list(TEST_KINDS)}")
                continue
            spec = TestSpec.from_dict({k: v for k, v in t.items() if k != "kind"} | {"kind": kind})
            exp_lower = {e.lower() for e in expected}
            for fld in ("value_col", "n_col", "group_col", "time_col", "part_col", "total_col"):
                v = getattr(spec, fld)
                if v is not None and str(v).lower() not in exp_lower:
                    errs.append(f"{cid}.test.{fld} {v!r} is not in expected_columns {expected}")
            if kind in ("group_diff", "before_after", "threshold") and not spec.value_col:
                errs.append(f"{cid}.test.value_col is required")
            if kind == "group_diff" and (not spec.group_col or spec.a is None or spec.b is None):
                errs.append(f"{cid}: group_diff needs group_col, a and b (b may be {REST!r})")
            if kind == "before_after" and (not spec.time_col or iso_period(spec.split_at) is None):
                errs.append(f"{cid}: before_after needs time_col and an ISO split_at date")
            if kind == "threshold":
                try:
                    float(spec.b)
                except (TypeError, ValueError):
                    errs.append(f"{cid}: threshold needs a numeric bound in b")
            if kind == "reconcile" and (not spec.part_col or not spec.total_col):
                errs.append(f"{cid}: reconcile needs part_col and total_col")
            if spec.direction not in ("higher", "lower"):
                errs.append(f"{cid}.test.direction must be 'higher' or 'lower'")
            if not str(rc.get("question", "")).strip():
                errs.append(f"{cid}: question is empty")
            cuts.append(Cut(cid, hid, "test", str(rc.get("question", "")).strip(), expected,
                            str(rc.get("grain") or "group"), spec))
        out.append(Hypothesis(hid, str(h.get("statement", "")).strip(), str(h.get("mechanism", "")).strip(),
                              claim, cuts, known_from=h.get("known_from")))
    if total_cuts > budget.max_cuts:
        errs.append(f"{total_cuts} cuts exceeds the budget of {budget.max_cuts}")
    return out, errs


def propose_hypotheses(question: str, ctx: GroundingContext, memory: Memory | None, budget: Budget, *,
                       model: str | None = None, today: str | None = None) -> list[Hypothesis]:
    from .._llm import complete
    from ..config import get_ai_settings

    system = planner_system(ctx, budget)
    user = planner_user(question, memory, today or date.today().isoformat())
    model = model or get_ai_settings().model_l1
    text, _ = complete(system=system, user=user, model=model, max_tokens=6000)
    errors: list[str] = []
    for attempt in (1, 2):
        try:
            payload = json.loads(strip_json(text), strict=False)
        except json.JSONDecodeError as exc:
            payload, errors = None, [f"output was not valid JSON: {exc}"]
        if payload is not None:
            hyps, errors = validate_plan(payload, ctx, budget)
            if not errors:
                return hyps
        if attempt == 2:
            break
        repair = (user + "\n\nYour previous plan was rejected by the validator:\n"
                  + "\n".join(f"  - {e}" for e in errors)
                  + "\n\nPrevious output:\n" + text + "\n\nReturn the corrected JSON only.")
        text, _ = complete(system=system, user=repair, model=model, max_tokens=6000)
    raise PlanError("planner output failed validation after one repair", errors, text)
