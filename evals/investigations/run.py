"""`evals/harness.py --investigate` modes (docs/investigate.md §8.1).

  --truths    (no key) every answer-key fixture_sql through L3-L5 on the BUILT
              warehouse; observed values must match. Proves the planted stories
              exist in what `up` built, not just in the generator's scratch copy.
  --replay    (no key) re-execute a recorded run or a reference plan's SQL through
              L3-L6, re-judge, re-skeptic, re-score.
  --live      (key) run real investigations: --config full|no_skeptic|no_memory|
              staging_only|B1|B2, --reps N, --questions Q1,Q3; or the memory
              protocol with --sequence Q6,Q1,Q3 [--no-memory]. Writes
              runs/investigations/eval_<ts>/scores.json + table.md. A hard stop on
              total spend (--max-total-usd, default 120) applies across the run.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

from engine.config import REPO_ROOT, has_anthropic_key
from engine.investigate.budget import Budget
from engine.investigate.runner import RUNS_DIR, investigate, load_investigation, replay
from engine.query.dry_run import dry_run_check
from engine.query.execution import guarded_execute
from engine.query.grounding import build_grounding_context, inline_synthetic_ctes
from engine.query.static_validation import validate_sql
from evals.investigations.score import aggregate, format_table, load_key, load_questions, score_investigation

TRUTH_EPS = 6e-5   # observed values are stored rounded to 4 dp


def schemas_present(dataset: str) -> bool:
    import duckdb

    from engine.config import WAREHOUSE_PATH
    if not WAREHOUSE_PATH.exists():
        return False
    con = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        have = {r[0] for r in con.execute("select schema_name from information_schema.schemata").fetchall()}
    finally:
        con.close()
    return {f"{dataset}_staging", f"{dataset}_marts"} <= have


def run_truths(dataset: str) -> list[tuple[str, bool, str]]:
    key = load_key(dataset)
    ctx = build_grounding_context(dataset)
    out = []
    for story in key["stories"]:
        for t in story.get("truths") or []:
            sql = t["fixture_sql"].strip()
            v = validate_sql(sql, ctx)
            if not v.ok:
                out.append((t["id"], False, f"L3 rejected: {[x.message for x in v.violations][:2]}"))
                continue
            ex = inline_synthetic_ctes(sql, ctx)
            dr = dry_run_check(ex)
            if not dr.ok:
                out.append((t["id"], False, f"L4: {dr.summary}"))
                continue
            o = guarded_execute(ex, dr)
            if not o.ok or not o.rows:
                out.append((t["id"], False, f"L5: {o.refusal.message if o.refusal else 'no rows'}"))
                continue
            got = list(o.rows[0].values())[0]
            obs = t.get("observed")
            if obs is None:
                out.append((t["id"], False, "observed is null — run datasets/saasco/generate.py"))
                continue
            ok = got is not None and abs(float(got) - float(obs)) <= TRUTH_EPS
            out.append((t["id"], ok, f"{story['id']} observed={obs} warehouse={round(float(got), 4) if got is not None else None}"))
    return out


def _eval_dir() -> Path:
    d = RUNS_DIR / f"eval_{datetime.now(timezone.utc):%Y-%m-%dT%H-%M-%S}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_one(question: str, dataset: str, config: str, ctx, out: Path, budget_usd: float):
    if config in ("B1", "B2"):
        from engine.investigate.baselines import run_b1, run_b2
        return (run_b1 if config == "B1" else run_b2)(question, dataset, ctx, out_dir=out)
    return investigate(question, dataset, config=config, ctx=ctx, out_dir=out, budget=Budget(max_usd=budget_usd))


def run_live(a) -> int:
    if not has_anthropic_key():
        print("[evals] --live needs an Anthropic key.")
        return 2
    out = _eval_dir()
    questions = load_questions(a.dataset)
    if a.questions:
        want = set(a.questions.split(","))
        questions = [q for q in questions if q["id"] in want]
    staging_only = a.config == "staging_only"
    ctx = build_grounding_context(a.dataset, schemas=[f"{a.dataset}_staging"] if staging_only else None)
    scores, spent = [], 0.0
    plan = []
    if a.sequence:
        by_id = {q["id"]: q for q in load_questions(a.dataset)}
        for rep in range(a.reps):
            plan.append(("seq", rep, [by_id[x] for x in a.sequence.split(",")]))
    else:
        for rep in range(a.reps):
            for q in questions:
                plan.append(("one", rep, [q]))
    for kind, rep, qs in plan:
        mem_path = out / "memory" / f"rep{rep}" / "findings.yml"
        if kind == "seq":
            os.environ["SEDIMENT_FINDINGS_PATH"] = str(mem_path)
            mem_path.parent.mkdir(parents=True, exist_ok=True)
        for pos, q in enumerate(qs, 1):
            if spent >= a.max_total_usd:
                print(f"[evals] HARD STOP: total spend ${spent:.2f} >= ${a.max_total_usd:.2f}")
                break
            config = a.config
            if kind == "seq":
                config = "no_memory" if a.no_memory else "full"
            print(f"[evals] rep {rep} · {q['id']} · {config}" + (f" · seq pos {pos}" if kind == "seq" else ""))
            inv = _run_one(q["question"], a.dataset, config, ctx, out / "runs", a.budget_usd)
            spent += float(inv.usage.get("usd") or 0)
            sc = score_investigation(inv, load_key(a.dataset), q) if config != "B1" else {
                "run_id": inv.run_id, "question_id": q["id"], "config": "B1", "human_only": True,
                "cost": {k: inv.usage.get(k) for k in ("calls", "usd", "wall_ms")}}
            sc["rep"], sc["sequence_pos"] = rep, (pos if kind == "seq" else None)
            scores.append(sc)
            if kind == "seq" and not a.no_memory:
                from engine.investigate.memory import accept, propose_delta
                prop = propose_delta(inv, mem_path.parent / "findings.proposed")
                added = accept(a.dataset, inv.run_id, auto=True, path=mem_path, proposed=prop)
                sc["memory_added"] = added
        os.environ.pop("SEDIMENT_FINDINGS_PATH", None)
    scored = [s for s in scores if not s.get("human_only")]
    agg = aggregate(scored) if scored else {"by_config": {}, "by_config_question": []}
    (out / "scores.json").write_text(json.dumps({"scores": scores, "aggregate": agg, "total_usd": spent},
                                                indent=2, default=str), encoding="utf-8")
    (out / "table.md").write_text(format_table(agg) + "\n", encoding="utf-8")
    print(format_table(agg))
    print(f"\n[evals] total spend ${spent:.2f} · {out / 'scores.json'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="investigate evals")
    ap.add_argument("--truths", action="store_true")
    ap.add_argument("--replay")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dataset", default="saasco")
    ap.add_argument("--config", default="full")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--questions")
    ap.add_argument("--sequence")
    ap.add_argument("--no-memory", action="store_true")
    ap.add_argument("--budget-usd", type=float, default=1.0)
    ap.add_argument("--max-total-usd", type=float, default=120.0)
    a = ap.parse_args(argv)

    if a.truths:
        if not schemas_present(a.dataset):
            print(f"[evals] {a.dataset}_staging/_marts not built. Run `python run.py up {a.dataset}`.")
            return 2
        res = run_truths(a.dataset)
        for tid, ok, detail in res:
            print(f"  [{'PASS' if ok else 'FAIL'}] {tid:8} {detail}")
        passed = sum(ok for _, ok, _ in res)
        print(f"\n[evals] truths over '{a.dataset}': {passed}/{len(res)} match the built warehouse")
        return 0 if res and passed == len(res) else 1

    if a.replay:
        src = Path(a.replay)
        if src.suffix in (".yaml", ".yml"):
            source = yaml.safe_load(src.read_text(encoding="utf-8"))
            ds = source["dataset"]
        else:
            source = load_investigation(src)
            ds = source.dataset
        inv = replay(source, build_grounding_context(ds), out_dir=RUNS_DIR)
        sc = score_investigation(inv)
        keep = ["question_id", "recall_confirmed", "recall_any", "found_confirmed", "relevant", "false_findings",
                "unkeyed_confirmed", "decoy", "traps", "quirks", "number_correctness", "traceability",
                "match_coverage"]
        print(json.dumps({k: sc[k] for k in keep}, indent=2, default=str))
        print(f"\nreport: {RUNS_DIR / inv.run_id / 'report.md'}")
        return 0

    if a.live:
        return run_live(a)
    ap.error("choose --truths, --replay or --live")
    return 2


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    raise SystemExit(main(sys.argv[1:]))
