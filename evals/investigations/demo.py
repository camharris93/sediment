"""Offline demo of the investigate layer — no API key, no LLM calls.

    python run.py up saasco && python run.py up saasco_holdout
    python evals/investigations/demo.py

What it shows, on the planted-truth dataset, using HAND-AUTHORED reference plans
(evals/investigations/reference_plans/ — written by someone who knows the
answers; they demonstrate the deterministic machinery, NOT that an LLM planner
finds the stories — only `harness.py --investigate --live` measures that):

  1. the planted stories are really in the built warehouse (truth replay);
  2. the judge alone CONFIRMS a Simpson's-paradox false finding; the skeptic's
     deterministic stratification REFUTES it;
  3. a small-n decoy gets confirmed by the judge and downgraded by the skeptic;
     a fan-out revenue number is flagged by L6 and never reaches "confirmed";
  4. every numeral in every report traces to an EvidenceCell.

Every number printed below is computed from the runs, not typed.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from engine.investigate.numfmt import fmt, pct  # noqa: E402
from engine.investigate.report import (  # noqa: E402
    _NUM,
    FINDINGS_END,
    FINDINGS_START,
    render,
    untraced_numbers,
)
from engine.investigate.runner import RUNS_DIR, replay  # noqa: E402
from engine.query.grounding import build_grounding_context  # noqa: E402
from evals.investigations.run import run_truths, schemas_present  # noqa: E402
from evals.investigations.score import score_investigation  # noqa: E402

PLANS = ROOT / "evals" / "investigations" / "reference_plans"
B, R = ("\033[1m", "\033[0m") if sys.stdout.isatty() else ("", "")


def _stats(inv, cut_id):
    return next(v.stats for v in inv.verdicts if v.cut_id == cut_id)


def _numerals(md: str) -> int:
    body = md.split(FINDINGS_START, 1)[1].split(FINDINGS_END, 1)[0]
    body = re.sub(r"<details>.*?</details>", " ", body, flags=re.S)
    return len(_NUM.findall(re.sub(r"`[^`]*`", " ", body)))


def main() -> int:
    for ds in ("saasco", "saasco_holdout"):
        if not schemas_present(ds):
            print(f"Build the data first:  python run.py up {ds}")
            return 2
    print(f"{B}sediment investigate — offline demo{R} (no API key, zero LLM calls)\n")

    print(f"{B}1. The planted stories are really in the warehouse{R}")
    for ds in ("saasco", "saasco_holdout"):
        res = run_truths(ds)
        print(f"   {ds:15} {sum(ok for _, ok, _ in res)}/{len(res)} answer-key truths reproduced through L3-L5")
    ctx = build_grounding_context("saasco")
    plan = lambda name: yaml.safe_load((PLANS / name).read_text(encoding="utf-8"))  # noqa: E731

    print(f"\n{B}2. Simpson's paradox — \"Does having a salesperson involved help retention?\"{R}")
    q4 = plan("q4_sales_assist_dev.yaml")
    judge_only = replay(q4, ctx, config="no_skeptic", out_dir=RUNS_DIR)
    full = replay(q4, ctx, out_dir=RUNS_DIR)
    s = _stats(full, "h1.c1")
    f1j, f1 = judge_only.findings[0], full.findings[0]
    print(f"   F1 \"{f1.statement}\"")
    print(f"   judge: sales-assisted {fmt(s['a'])} vs {fmt(s['b'])} monthly churn ({pct(s['rel'])}, "
          f"n = {fmt(s['n_a'])} vs {fmt(s['n_b'])}) -> {B}{f1j.label.upper()}{R}")
    sim = next(c for c in f1.skeptic if c.kind == "simpsons")
    print(f"   skeptic simpsons on `{sim.confounder}`: {sim.reason}")
    print(f"   -> {B}{f1.label.upper()}{R}.  False findings (scored vs answer key): "
          f"judge-only {score_investigation(judge_only)['n_false_findings']}, "
          f"with skeptic {score_investigation(full)['n_false_findings']}")
    f2 = full.findings[1]
    s2 = _stats(full, "h2.c1")
    print(f"   F2 \"{f2.statement}\" -> {f2.label.upper()} "
          f"(1-10 band: {fmt(s2['a'])} vs {fmt(s2['b'])}, {pct(s2['rel'])})")

    print(f"\n{B}3. Decoy and fan-out — \"Why is churn up in Q2 2026?\"{R}")
    q1 = replay(plan("q1_why_churn_up_dev.yaml"), ctx, out_dir=RUNS_DIR)
    by = {f.hypothesis_id: f for f in q1.findings}
    s1 = _stats(q1, "h1.c1")
    print(f"   annual accounts in renewal month churn {fmt(s1['a'])} vs {fmt(s1['b'])} monthly-billed "
          f"({pct(s1['rel'])}) -> {by['h1'].label.upper()}, skeptic {by['h1'].skeptic_outcome} "
          f"({', '.join(c.kind for c in by['h1'].skeptic)})")
    s3 = _stats(q1, "h3.c1")
    small = next(c for c in by["h3"].skeptic if c.kind == "small_n")
    print(f"   healthcare Q1 churn {fmt(s3['a'])} vs {fmt(s3['b'])} ({pct(s3['rel'])}, n = {fmt(s3['n_a'])}) "
          f"-> judge {by['h3'].judged_label.upper()}; skeptic small_n: {small.reason} "
          f"-> {B}{by['h3'].label.upper()}{R}")
    r4 = q1.result("h4.c1")
    print(f"   lost MRR per churned subscription {pct(_stats(q1, 'h4.c1')['rel'])} after 2026 -> L6 "
          f"{sorted({w['kind'] for w in r4.warnings})} (subscriptions x invoice ATTEMPTS) -> badge {r4.trust_badge} "
          f"-> {B}{by['h4'].label.upper()}{R}, never confirmed")
    sc = score_investigation(q1)
    print(f"   score: recall@confirmed {sc['recall_confirmed']} of {sc['relevant']} · false findings "
          f"{sc['n_false_findings']} · decoy {sc['decoy']} · fan-out flagged {sc['quirks']['S7']['flagged']}")

    print(f"\n{B}4. Traceability{R}")
    q3 = replay(plan("q3_europe_dev.yaml"), ctx, out_dir=RUNS_DIR)
    total = bad = 0
    for inv in (full, q1, q3):
        md = render(inv)
        total += _numerals(md)
        bad += len(untraced_numbers(md, inv))
    print(f"   {total} numerals across 3 reports' findings; {bad} not traceable to an EvidenceCell")

    print(f"\n{B}Reports{R} (open these):")
    for inv in (full, q1, q3):
        print(f"   {(RUNS_DIR / inv.run_id / 'report.md').relative_to(ROOT)}")
    print("\nLive (needs a key): python run.py investigate \"why is churn up in Q2 2026?\" saasco")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
