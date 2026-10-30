"""Eval harness for the NL->SQL agent.

Two modes:

  • replay (default, NO key, CI-safe): runs each golden question's known-good
    `fixture_sql` through the DETERMINISTIC trust layers — L3 static validation,
    L4 dry-run, L5 guarded execution, L6 plausibility — and checks the RESULT
    against the case's invariants. Adversarial cases assert the guard refuses.
    This regression-tests the layers and the curated data values without an LLM.

  • live (--live, needs ANTHROPIC_API_KEY): runs the FULL L1->L7 pipeline from the
    natural-language question and checks the same invariants on the answer. This is
    the end-to-end accuracy measurement; print the pass rate in the README.

Both modes ground against the real warehouse (built by `python run.py up`).

    python evals/harness.py            # replay
    python evals/harness.py --live     # full pipeline (key required)
    python evals/harness.py --cases path/to/other_golden.yaml

`--investigate` evaluates the investigation layer against planted truth
(docs/investigate.md §8; implementation in evals/investigations/):

    python evals/harness.py --investigate --truths [--dataset saasco]      # no key, CI-safe
    python evals/harness.py --investigate --replay <run dir | reference plan .yaml>   # no key
    python evals/harness.py --investigate --live --config full --reps 3 [--questions Q1,Q3]
    python evals/harness.py --investigate --live --config B2 --dataset saasco_holdout
    python evals/harness.py --investigate --live --sequence Q6,Q1,Q3 [--no-memory]
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import WAREHOUSE_PATH  # noqa: E402
from engine.investigate.evaluate import check_invariants  # noqa: E402,F401
from engine.query.dry_run import dry_run_check  # noqa: E402
from engine.query.execution import guarded_execute  # noqa: E402
from engine.query.grounding import build_grounding_context, inline_synthetic_ctes  # noqa: E402
from engine.query.static_validation import validate_sql  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden_questions.yaml"


@dataclass
class CaseResult:
    id: str
    ok: bool
    detail: str = ""


def load_cases(path: Path | None = None) -> list[dict]:
    data = yaml.safe_load((path or GOLDEN).read_text(encoding="utf-8"))
    return data["questions"]


# Invariant checking lives in engine.investigate.evaluate (shared with the judge).


# ── Replay mode: deterministic layers only, no LLM ───────────────────────────

def run_replay(grounding, cases: list[dict]) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in cases:
        cid = case["id"]
        sql = case["fixture_sql"].strip()

        if case.get("refuse"):
            # Adversarial: the guard must refuse before touching the database.
            outcome = guarded_execute(sql, dry_run_check(inline_synthetic_ctes(sql, grounding)))
            ok = not outcome.ok
            detail = "refused as required" if ok else "SECURITY: guard did NOT refuse"
            results.append(CaseResult(cid, ok, detail))
            continue

        # L3 static validation
        v = validate_sql(sql, grounding)
        if not v.ok:
            results.append(CaseResult(cid, False,
                           f"L3 rejected fixture: {[x.message for x in v.violations]}"))
            continue
        # L4 dry-run + L5 guarded execution
        executable = inline_synthetic_ctes(sql, grounding)
        dr = dry_run_check(executable)
        if not dr.ok:
            results.append(CaseResult(cid, False, f"L4 dry-run failed: {dr.summary}"))
            continue
        outcome = guarded_execute(executable, dr)
        if not outcome.ok:
            results.append(CaseResult(cid, False,
                           f"L5 refused: {outcome.refusal.message if outcome.refusal else '?'}"))
            continue
        ok, detail = check_invariants(outcome.rows, case.get("expect"))
        results.append(CaseResult(cid, ok, detail or f"{len(outcome.rows)} rows"))
    return results


# ── Live mode: full L1->L7 pipeline (needs a key) ────────────────────────────

def run_live(grounding, cases: list[dict]) -> list[CaseResult]:
    from engine.query.orchestrator import run_to_executed_answer

    results: list[CaseResult] = []
    for case in cases:
        cid = case["id"]
        if case.get("refuse"):
            continue  # adversarial SQL isn't reachable via NL generation
        res = run_to_executed_answer(case["question"], grounding)
        if res.status != "executed":
            results.append(CaseResult(cid, False, f"pipeline status={res.status} (no answer)"))
            continue
        ok, detail = check_invariants(res.rows, case.get("expect"))
        results.append(CaseResult(cid, ok, detail or f"{len(res.rows)} rows"))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run NL->SQL golden evals.")
    ap.add_argument("--live", action="store_true",
                    help="run the full L1->L7 pipeline (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--dataset", default=None, help="default: anage (saasco with --investigate)")
    ap.add_argument("--cases", help="golden cases YAML (default: evals/golden_questions.yaml)")
    ap.add_argument("--investigate", action="store_true", help="evaluate the investigation layer")
    args, rest = ap.parse_known_args(argv)

    if not WAREHOUSE_PATH.exists():
        print(f"[evals] no warehouse at {WAREHOUSE_PATH}. Run `python run.py up` first.")
        return 2
    if args.investigate:
        from evals.investigations.run import main as investigate_main
        return investigate_main(rest + (["--live"] if args.live else [])
                                + (["--dataset", args.dataset] if args.dataset else []))
    if rest:
        ap.error(f"unrecognized arguments: {' '.join(rest)}")
    args.dataset = args.dataset or "anage"

    cases = load_cases(Path(args.cases) if args.cases else None)
    grounding = build_grounding_context(args.dataset)
    mode = "live (full L1->L7)" if args.live else "replay (deterministic L3-L6)"
    runner = run_live if args.live else run_replay
    print(f"[evals] {mode} — {len(cases)} golden cases over '{args.dataset}'\n")

    results = runner(grounding, cases)
    passed = sum(1 for r in results if r.ok)
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        print(f"  [{mark}] {r.id:28} {r.detail}")

    counted = len(results)
    print(f"\n[evals] {passed}/{counted} passed"
          + (f"  ({100*passed//counted}%)" if counted else ""))
    return 0 if passed == counted and counted > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
