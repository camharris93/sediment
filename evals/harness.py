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


def load_cases() -> list[dict]:
    data = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    return data["questions"]


# ── Invariant checking (shared by both modes) ────────────────────────────────

def check_invariants(rows: list[dict], expect: dict) -> tuple[bool, str]:
    if expect is None:
        return True, ""
    if "row_count" in expect and len(rows) != expect["row_count"]:
        return False, f"expected {expect['row_count']} rows, got {len(rows)}"
    if "min_rows" in expect and len(rows) < expect["min_rows"]:
        return False, f"expected >= {expect['min_rows']} rows, got {len(rows)}"
    if "columns" in expect:
        present = set(rows[0].keys()) if rows else set()
        missing = [c for c in expect["columns"] if c not in present]
        if missing:
            return False, f"missing expected columns {missing} (got {sorted(present)})"
    if "first_row" in expect:
        if not rows:
            return False, "expected a first row, got none"
        for col, want in expect["first_row"].items():
            got = rows[0].get(col)
            if isinstance(want, (int, float)) and isinstance(got, (int, float)):
                if abs(float(got) - float(want)) > 1e-6:
                    return False, f"first_row.{col}: expected {want}, got {got}"
            elif got != want:
                return False, f"first_row.{col}: expected {want!r}, got {got!r}"
    if "all_positive" in expect:
        for col in expect["all_positive"]:
            for r in rows:
                v = r.get(col)
                if v is None or float(v) <= 0:
                    return False, f"all_positive.{col}: found non-positive value {v!r}"
    return True, ""


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
    ap.add_argument("--dataset", default="anage")
    args = ap.parse_args(argv)

    if not WAREHOUSE_PATH.exists():
        print(f"[evals] no warehouse at {WAREHOUSE_PATH}. Run `python run.py up` first.")
        return 2

    cases = load_cases()
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
