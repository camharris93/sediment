"""NL->SQL CLI over the marts. One-shot or interactive.

    python -m engine.query.cli "which animals live longest for their size?"
    python -m engine.query.cli            # interactive REPL

Prints the layer trace as it runs, then the SQL, the answer, and a trust badge.
Needs an Anthropic key (the consumption edge is the one place AI is essential);
the deterministic core never calls it.
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from ..config import WAREHOUSE_PATH, active_dataset, has_anthropic_key
from .grounding import build_grounding_context
from .orchestrator import run_to_executed_answer
from .trace import TraceEvent

_BADGE_ICON = {
    "clean": "✅ clean", "self_corrected": "🔧 self-corrected",
    "flagged": "⚠️  flagged", "refused": "⛔ refused", "failed": "❌ failed",
}


def _trace_printer() -> "callable":
    def on_event(ev: TraceEvent) -> None:
        layer = ev.layer.value if ev.layer else "--"
        if ev.kind == "layer_start":
            print(f"  [{layer}] ...", flush=True)
        elif ev.kind == "layer_result":
            if ev.layer and ev.layer.value == "L2":
                print(f"  [{layer}] generated SQL ({ev.payload.get('attempt')})")
            elif ev.payload.get("pass"):
                extra = ""
                if "row_count" in ev.payload:
                    extra = f" ({ev.payload['row_count']} rows, {ev.payload.get('elapsed_ms',0)}ms)"
                print(f"  [{layer}] pass{extra}")
        elif ev.kind == "validation_fail":
            detail = ev.payload.get("error_summary") or ev.payload.get("error") \
                or ev.payload.get("violations") or ev.payload.get("warnings") \
                or ev.payload.get("refusal") or ""
            print(f"  [{layer}] FAIL: {str(detail)[:160]}")
        elif ev.kind == "retry":
            print(f"  [{layer}] -> retry {ev.payload.get('next_attempt')}")
    return on_event


def answer(question: str, ctx=None) -> int:
    ctx = ctx or build_grounding_context(active_dataset())
    print(f"\nQ: {question}\n" + "-" * 70)
    result = run_to_executed_answer(question, ctx, on_event=_trace_printer())
    fa = result.final_answer
    print("-" * 70)
    if result.sql:
        print("\nSQL:\n" + result.sql + "\n")
    if fa:
        if fa.assumptions:
            print("Assumptions:")
            for a in fa.assumptions:
                print(f"  - {a}")
        print("\nAnswer: " + fa.explanation)
        if fa.plausibility_warnings:
            print("\nPlausibility flags:")
            for w in fa.plausibility_warnings:
                print(f"  - {w['message']}")
        if result.rows:
            print(f"\nRows ({fa.row_count} returned, showing up to 10):")
            keys = list(result.rows[0].keys())
            print("  " + " | ".join(keys))
            for r in result.rows[:10]:
                print("  " + " | ".join(str(r[k]) for k in keys))
        badge = _BADGE_ICON.get(fa.trust_badge.value, fa.trust_badge.value)
        print(f"\nTrust: {badge}   |   attempts: {result.attempts}   |   {fa.elapsed_ms}ms")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not WAREHOUSE_PATH.exists():
        print("No warehouse found. Run `python run.py up` first.")
        return 1
    if not has_anthropic_key():
        print(
            "The NL->SQL query agent needs an Anthropic key (consumption edge).\n"
            "Set ANTHROPIC_API_KEY or add a one-line anthropic.txt at the repo root.\n"
            "The deterministic core (make up, dashboard) works without it."
        )
        return 1

    ds = active_dataset()
    print(f"Grounding schema for dataset '{ds}' from DuckDB...")
    ctx = build_grounding_context(ds)
    print(f"  grounded {len(ctx.tables)} tables, {len(ctx.relationships)} relationships.")

    if argv:
        return answer(" ".join(argv), ctx)

    print("\nInteractive NL->SQL. Ask a question (empty line or Ctrl-C to quit).")
    try:
        while True:
            q = input("\n> ").strip()
            if not q:
                break
            answer(q, ctx)
    except (EOFError, KeyboardInterrupt):
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
