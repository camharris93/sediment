"""`investigate` CLI.

    python -m engine.investigate.cli "why is churn up in Q2 2026?" --dataset saasco
    python -m engine.investigate.cli "..." --dataset saasco --config no_skeptic --budget-usd 0.5
    python -m engine.investigate.cli "..." --dataset saasco --baseline B2
    python -m engine.investigate.cli --replay runs/investigations/<run_id>       # no key
    python -m engine.investigate.cli --replay evals/investigations/reference_plans/q4_sales_assist_dev.yaml

Live runs need an Anthropic key; replay never calls an LLM.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import yaml

from ..config import has_anthropic_key
from ..query.grounding import build_grounding_context
from ..query.trace import TraceEvent
from .budget import Budget
from .runner import CONFIGS, RUNS_DIR, investigate, load_investigation, replay

_MARK = {"confirmed": "[CONFIRMED]", "suggestive": "[suggestive]", "refuted": "[refuted]",
         "untestable": "[untestable]", "passed": "passed", "failed": "FAILED"}


def printer():
    def on_event(ev: TraceEvent) -> None:
        p, k = ev.payload, ev.kind
        if k == "plan_proposed":
            print(f"  plan: {len(p.get('hypotheses', []))} hypotheses")
            for h in p.get("hypotheses", []):
                print(f"    {h['id']}: {h['statement'][:90]}")
        elif k == "cut_start":
            print(f"  -> {p.get('cut_id')}: {str(p.get('question', ''))[:80]}")
        elif k == "cut_result":
            print(f"     {p.get('status')} · badge {p.get('badge')} · {p.get('rows')} rows · {p.get('attempts')} attempt(s)")
        elif k == "verdict":
            print(f"     {p.get('cut_id')} {_MARK.get(p.get('label'), p.get('label'))}: {str(p.get('reason'))[:100]}")
        elif k == "skeptic_result":
            print(f"  skeptic {p.get('check')} on {p.get('cut_id')}: {_MARK.get(p.get('outcome'), p.get('outcome'))}"
                  f" — {str(p.get('reason'))[:90]}")
        elif k == "budget_stop":
            print(f"  [budget] {p.get('reason')}: skipping {p.get('cut_id')}")
    return on_event


def _summary(inv, out: Path | None) -> None:
    print("\nFindings:")
    for f in inv.findings:
        moved = f" (judge: {f.judged_label}, skeptic: {f.skeptic_outcome})" if f.judged_label != f.label else ""
        print(f"  {_MARK.get(f.label, f.label):13} {f.id}: {f.statement[:90]}{moved}")
    u = inv.usage or {}
    print(f"\nCost: {u.get('calls', 0)} LLM calls · ${u.get('usd', 0):.4f} · {u.get('queries', 0)} cuts · "
          f"{(u.get('wall_ms') or 0) / 1000:.1f}s")
    if inv.error:
        print(f"[error] {inv.error}")
    if out is not None:
        print(f"Report: {out / inv.run_id / 'report.md'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Investigate an open analytical question.")
    ap.add_argument("question", nargs="?")
    ap.add_argument("--dataset", default="saasco")
    ap.add_argument("--config", default="full", choices=sorted(CONFIGS))
    ap.add_argument("--no-skeptic", action="store_true", help="alias for --config no_skeptic")
    ap.add_argument("--no-memory", action="store_true", help="alias for --config no_memory")
    ap.add_argument("--budget-usd", type=float, default=1.0)
    ap.add_argument("--baseline", choices=["B1", "B2"])
    ap.add_argument("--replay", help="a recorded run dir/investigation.json, or a reference plan .yaml")
    ap.add_argument("--out", default=str(RUNS_DIR))
    a = ap.parse_args(argv)
    out = Path(a.out)

    if a.replay:
        src = Path(a.replay)
        if src.suffix in (".yaml", ".yml"):
            plan = yaml.safe_load(src.read_text(encoding="utf-8"))
            ds = plan["dataset"]
            source = plan
        else:
            source = load_investigation(src)
            ds = source.dataset
        print(f"[investigate] replay of {src} over '{ds}' (no LLM)")
        inv = replay(source, build_grounding_context(ds), out_dir=out, on_event=printer())
        _summary(inv, out)
        return 0

    if not a.question:
        ap.error("a question is required (or --replay)")
    if not has_anthropic_key():
        print("[investigate] no Anthropic key: set ANTHROPIC_API_KEY or anthropic.txt. "
              "Key-free alternative: --replay <reference plan or recorded run>.")
        return 2
    config = "no_skeptic" if a.no_skeptic else "no_memory" if a.no_memory else a.config
    ctx = build_grounding_context(a.dataset, schemas=[f"{a.dataset}_staging"] if config == "staging_only" else None)
    if a.baseline:
        from .baselines import run_b1, run_b2
        inv = (run_b1 if a.baseline == "B1" else run_b2)(a.question, a.dataset, ctx, out_dir=out)
        _summary(inv, out)
        return 0
    print(f"[investigate] '{a.question}' over '{a.dataset}' · config {config} · budget ${a.budget_usd:.2f}")
    inv = investigate(a.question, a.dataset, config=config, budget=Budget(max_usd=a.budget_usd), ctx=ctx,
                      out_dir=out, on_event=printer())
    _summary(inv, out)
    return 0 if not inv.error else 1


if __name__ == "__main__":
    raise SystemExit(main())
