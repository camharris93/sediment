"""The investigation loop (docs/investigate.md §3).

    0. ctx once; memory once (unless the config turns it off)
    1. PLAN    one LLM call -> hypotheses + cuts + TestSpecs (validated)
    2. CUT     each cut through the full L1->L7 pipeline (explain=False)
    3. JUDGE   deterministic verdicts; hypothesis label = combine(verdicts)
    4. SKEPTIC confirmed findings only: LLM picks <= 2 checks; deterministic outcomes
    5. REPORT  template markdown + investigation.json + trace.jsonl
    6. PROPOSE memory delta -> findings.proposed/<run_id>.yml (a human accepts)

The planner, cut runner and check chooser are injectable, which is how replay
(recorded SQL, no LLM) and the offline tests reuse this exact loop.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._llm import usage_meter
from ..config import REPO_ROOT
from ..query.grounding import GroundingContext, build_grounding_context
from ..query.trace import TraceEvent
from .budget import Budget, BudgetTracker
from .cuts import json_safe, run_cut, run_cut_sql
from .evaluate import DEFAULT_JUDGE, JudgeConfig, combine, evaluate
from .memory import Memory, load_findings, propose_delta
from .plan import PlanError, propose_hypotheses
from .report import render, to_json
from .skeptic import build_skeptic_cut, choose_checks, outcome_of, run_check
from .types import Cut, CutResult, Finding, Hypothesis, Investigation, Verdict

RUNS_DIR = REPO_ROOT / "runs" / "investigations"


@dataclass(frozen=True)
class RunConfig:
    name: str
    skeptic: bool = True
    memory: bool = True
    staging_only: bool = False


CONFIGS: dict[str, RunConfig] = {
    "full": RunConfig("full"),
    "no_skeptic": RunConfig("no_skeptic", skeptic=False),
    "no_memory": RunConfig("no_memory", memory=False),
    "staging_only": RunConfig("staging_only", staging_only=True),
}

Planner = Callable[..., list[Hypothesis]]
CutRunner = Callable[..., CutResult]
Chooser = Callable[..., list[dict]]


def make_run_id(question: str, config: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:32].strip("-")
    h = hashlib.sha1(f"{question}|{config}|{now.isoformat()}".encode()).hexdigest()[:6]
    return f"{now:%Y-%m-%dT%H-%M-%S}_{slug}_{config}_{h}"


def _emit(on_event, trace: list[dict], kind: str, **payload) -> None:
    trace.append({"cut_id": payload.get("cut_id"), "kind": kind, "layer": None,
                  "payload": {k: json_safe(v) if not isinstance(v, (list, dict)) else v for k, v in payload.items()}})
    if on_event:
        try:
            on_event(TraceEvent(kind=kind, layer=None, payload=payload))
        except Exception:
            pass


def _skipped(cut_id: str, reason: str) -> CutResult:
    return CutResult(cut_id, "skipped", None, [], "failed", error=reason)


def _finding_for(h: Hypothesis, verdicts: list[Verdict], results: dict[str, CutResult]) -> Finding:
    label = combine([v.label for v in verdicts])
    evidence = [c for v in verdicts if v.label != "untestable" for c in v.evidence]
    sql = [results[v.cut_id].sql for v in verdicts if v.cut_id in results and results[v.cut_id].sql]
    note = None
    if label == "untestable":
        note = "; ".join(f"{v.cut_id}: {v.reason}" for v in verdicts)
    if h.known_from:
        note = (note + " · " if note else "") + f"restates memory {h.known_from}; re-tested here"
    return Finding(id=f"F{h.id[1:]}", hypothesis_id=h.id, label=label, statement=h.statement, claim=h.claim,
                   evidence=evidence, sql=sql, judged_label=label, note=note)


def investigate(question: str, dataset: str, *, config: str = "full", budget: Budget | None = None,
                judge: JudgeConfig = DEFAULT_JUDGE, ctx: GroundingContext | None = None,
                memory: Memory | None = None, out_dir: Path | None = RUNS_DIR,
                on_event: Callable[[TraceEvent], None] | None = None,
                planner: Planner = propose_hypotheses, cut_runner: CutRunner = run_cut,
                chooser: Chooser = choose_checks, mode: str = "live", run_id: str | None = None,
                propose: bool = True) -> Investigation:
    rc = CONFIGS[config] if config in CONFIGS else RunConfig(config)
    budget = budget or Budget()
    run_id = run_id or make_run_id(question, rc.name)
    inv = Investigation(run_id=run_id, question=question, dataset=dataset, config=rc.name, mode=mode)
    trace: list[dict] = []

    if ctx is None:
        schemas = [f"{dataset}_staging"] if rc.staging_only else None
        ctx = build_grounding_context(dataset, schemas=schemas)
    if memory is None:
        memory = load_findings(dataset) if rc.memory else Memory(dataset)
    elif not rc.memory:
        memory = Memory(dataset)
    inv.memory_in = memory.sha
    definitions = memory.definitions

    with usage_meter() as meter:
        tracker = BudgetTracker(budget, meter)
        try:
            inv.hypotheses = planner(question, ctx, memory, budget)
        except PlanError as exc:
            inv.error = f"planner failed: {exc} {exc.errors}"
        except Exception as exc:
            inv.error = f"planner failed: {type(exc).__name__}: {exc}"
        _emit(on_event, trace, "plan_proposed",
              hypotheses=[{"id": h.id, "statement": h.statement, "cuts": [c.question for c in h.cuts]}
                          for h in inv.hypotheses])

        results: dict[str, CutResult] = {}
        for h in inv.hypotheses:
            for cut in h.cuts:
                stop = tracker.exhausted()
                if stop:
                    inv.stopped_reason = inv.stopped_reason or stop
                    results[cut.id] = _skipped(cut.id, stop)
                    _emit(on_event, trace, "budget_stop", cut_id=cut.id, reason=stop)
                    continue
                _emit(on_event, trace, "cut_start", cut_id=cut.id, question=cut.question)
                results[cut.id] = cut_runner(cut, ctx, definitions, on_event=on_event, trace=trace)
                tracker.note_query()
                r = results[cut.id]
                _emit(on_event, trace, "cut_result", cut_id=cut.id, status=r.status, badge=r.trust_badge,
                      rows=len(r.rows), attempts=r.attempts)
        inv.cut_results = list(results.values())

        for h in inv.hypotheses:
            vs = [evaluate(c, results.get(c.id), judge) for c in h.cuts]
            inv.verdicts += vs
            for v in vs:
                _emit(on_event, trace, "verdict", cut_id=v.cut_id, label=v.label, reason=v.reason)
            inv.findings.append(_finding_for(h, vs, results))

        if rc.skeptic:
            skeptic_used = 0
            for f in inv.findings:
                if f.label != "confirmed":
                    continue
                h = next(x for x in inv.hypotheses if x.id == f.hypothesis_id)
                try:
                    checks = chooser(f, h, results, ctx, memory)
                except Exception as exc:
                    checks = []
                    f.note = (f.note + " · " if f.note else "") + f"skeptic chooser failed: {exc}"
                for k, check in enumerate(checks, 1):
                    target = next((c for c in h.cuts if c.id == check["target_cut_id"]), h.cuts[0])
                    scut = build_skeptic_cut(check, target, f"{h.id}.s{k}")
                    if scut is not None:
                        stop = tracker.exhausted() or (
                            "budget_skeptic_cuts" if skeptic_used >= budget.max_skeptic_cuts else None)
                        inv.skeptic_cuts.append(scut)
                        if stop:
                            results[scut.id] = _skipped(scut.id, stop)
                            inv.stopped_reason = inv.stopped_reason or stop
                        else:
                            _emit(on_event, trace, "cut_start", cut_id=scut.id, question=scut.question)
                            results[scut.id] = cut_runner(scut, ctx, definitions, on_event=on_event, trace=trace)
                            tracker.note_query()
                            skeptic_used += 1
                        inv.cut_results.append(results[scut.id])
                    sc = run_check(check, target, results, scut, judge)
                    f.skeptic.append(sc)
                    _emit(on_event, trace, "skeptic_result", cut_id=target.id, check=sc.kind,
                          outcome=sc.outcome, reason=sc.reason)
                outcome, new_label = outcome_of(f.skeptic)
                f.skeptic_outcome = outcome
                if new_label:
                    f.label = new_label
                if f.skeptic and outcome == "not_run":
                    f.note = (f.note + " · " if f.note else "") + "skeptic could not test"

        restated = {h.known_from for h in inv.hypotheses if h.known_from}
        inv.known = [{"id": m["id"], "statement": m["statement"]} for m in memory.findings
                     if m["id"] not in restated]
        inv.usage = tracker.summary()

    if out_dir is not None:
        write_run(inv, out_dir, trace, judge)
        if propose and rc.memory and inv.findings:
            propose_delta(inv)
    return inv


def write_run(inv: Investigation, out_dir: Path, trace: list[dict] | None = None,
              judge: JudgeConfig = DEFAULT_JUDGE) -> Path:
    d = out_dir / inv.run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "investigation.json").write_text(to_json(inv), encoding="utf-8")
    (d / "report.md").write_text(render(inv, judge), encoding="utf-8")
    if trace is not None:
        with (d / "trace.jsonl").open("w", encoding="utf-8") as fh:
            for ev in trace:
                fh.write(json.dumps(ev, default=str) + "\n")
    return d


# ── Replay: the same loop, with SQL supplied instead of generated ────────────

def replay(source: Investigation | dict, ctx: GroundingContext, *, judge: JudgeConfig = DEFAULT_JUDGE,
           out_dir: Path | None = None, run_id: str | None = None, config: str | None = None,
           dataset: str | None = None, on_event: Callable[[TraceEvent], None] | None = None) -> Investigation:
    """Re-run an investigation from SQL alone — a recorded run (investigation.json)
    or a hand-authored plan (hypotheses whose cuts carry `sql`, plus `skeptic`
    checks with `sql`). L3-L6, the judge and the skeptic check functions all run;
    no LLM does."""
    if isinstance(source, Investigation):
        inv0 = source
        sql = {r.cut_id: r.sql for r in inv0.cut_results if r.sql}
        sql.update({c.id: c.sql for c in inv0.all_cuts() if c.sql})
        checks = {f.hypothesis_id: [{"kind": s.kind, "target_cut_id": s.target_cut_id,
                                     "confounder": s.confounder, "alt_definition": s.alt_definition}
                                    for s in f.skeptic] for f in inv0.findings}
        hyps, question, ds, cfg = inv0.hypotheses, inv0.question, inv0.dataset, inv0.config
    else:
        hyps = [Hypothesis.from_dict(h) for h in source["hypotheses"]]
        sql = {c.id: c.sql for h in hyps for c in h.cuts if c.sql}
        checks = {}
        for h in source["hypotheses"]:
            for k, s in enumerate(h.get("skeptic", []) or [], 1):
                checks.setdefault(h["id"], []).append({kk: s.get(kk) for kk in
                                                       ("kind", "target_cut_id", "confounder", "alt_definition")})
                if s.get("sql"):
                    sql[f"{h['id']}.s{k}"] = s["sql"]
        question, ds, cfg = source["question"], source["dataset"], source.get("config", "full")

    def planner(*_a, **_k):
        return [Hypothesis.from_dict(json.loads(json.dumps(_asdict(h), default=str))) for h in hyps]

    def cut_runner(cut: Cut, ctx_, _defs, **_k) -> CutResult:
        s = sql.get(cut.id)
        if not s:
            return CutResult(cut.id, "transparent_failure", None, [], "failed", error="no recorded SQL")
        return run_cut_sql(cut.id, s, ctx_)

    def chooser(f: Finding, *_a, **_k) -> list[dict]:
        return checks.get(f.hypothesis_id, [])

    return investigate(question, dataset or ds, config=config or cfg, judge=judge, ctx=ctx,
                       memory=Memory(dataset or ds), out_dir=out_dir, planner=planner, on_event=on_event,
                       cut_runner=cut_runner, chooser=chooser, mode="replay",
                       run_id=run_id or f"replay_{make_run_id(question, cfg)}", propose=False,
                       budget=Budget(max_queries=10_000, max_usd=1e9, max_wall_s=1e9,
                                     max_input_tokens=10**12, max_output_tokens=10**12,
                                     max_skeptic_cuts=10_000))


def _asdict(obj: Any) -> Any:
    from dataclasses import asdict
    return asdict(obj)


def load_investigation(path: Path) -> Investigation:
    p = path / "investigation.json" if path.is_dir() else path
    return Investigation.from_dict(json.loads(p.read_text(encoding="utf-8")))
