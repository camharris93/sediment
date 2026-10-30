"""Deterministic scoring of investigations against a planted-truth answer key
(docs/investigate.md §8.3). No LLM anywhere in here.

A finding is matched to a story by its structured Claim (match grammar at the top
of each answer key). Headline metrics: Recall@confirmed and False findings.
"""
from __future__ import annotations

import fnmatch
import math
import re
from pathlib import Path
from typing import Any

import yaml

from engine.investigate.memory import build_delta
from engine.investigate.report import render, untraced_numbers
from engine.investigate.types import Claim, Finding, Investigation

HERE = Path(__file__).resolve().parent
KEYS = {"saasco": HERE / "saasco_answer_key.yaml", "saasco_holdout": HERE / "saasco_holdout_answer_key.yaml"}
QUESTIONS = HERE / "saasco_questions.yaml"
RECALL_KINDS = ("driver", "trap")


def load_key(dataset: str) -> dict:
    return yaml.safe_load(KEYS[dataset].read_text(encoding="utf-8"))


def load_questions(dataset: str) -> list[dict]:
    variant = "holdout" if dataset.endswith("_holdout") else "dev"
    out = []
    for q in yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))["questions"]:
        merged = {k: v for k, v in q.items() if k not in ("holdout",)}
        if variant == "holdout":
            merged.update(q.get("holdout") or {})
        out.append(merged)
    return out


def question_entry(dataset: str, question: str) -> dict | None:
    norm = question.strip().lower()
    return next((q for q in load_questions(dataset) if q["question"].strip().lower() == norm), None)


# ── matching ─────────────────────────────────────────────────────────────────

def _eq(a: Any, b: Any) -> bool:
    return str(a).strip().lower() == str(b).strip().lower()


def _dim_hit(pattern: str, dims: list[str]) -> bool:
    return any(fnmatch.fnmatch(d.lower(), str(pattern).lower()) for d in dims)


def match_sig(claim: Claim, sig: dict) -> bool:
    dims = [str(d) for d in claim.dimensions] + [str(k) for k in claim.segment]
    if "metric_in" in sig and claim.metric not in sig["metric_in"]:
        return False
    if "dimension_any" in sig and not any(_dim_hit(p, dims) for p in sig["dimension_any"]):
        return False
    if "dimension_all" in sig and not all(_dim_hit(p, dims) for p in sig["dimension_all"]):
        return False
    for k, v in (sig.get("segment") or {}).items():
        if not any(fnmatch.fnmatch(str(ck).lower(), str(k).lower()) and _eq(cv, v)
                   for ck, cv in claim.segment.items()):
            return False
    if "direction" in sig and claim.direction != sig["direction"]:
        return False
    if "direction_any" in sig and claim.direction not in sig["direction_any"]:
        return False
    return "quirk_tables" not in sig


def classify(f: Finding, key: dict) -> list[tuple[str, str]]:
    """[(story_id, 'correct'|'naive')] this finding matches."""
    out = []
    for s in key["stories"]:
        if s["kind"] == "quirk":
            continue
        correct = s.get("match_correct") or s.get("match") or []
        if any(match_sig(f.claim, sig) for sig in correct):
            out.append((s["id"], "correct"))
        elif any(match_sig(f.claim, sig) for sig in s.get("match_naive") or []):
            out.append((s["id"], "naive"))
    return out


# ── S7-style quirk handling ──────────────────────────────────────────────────

_SUM_AMOUNT = re.compile(r"sum\s*\([^)]*amount", re.I)


def _naive_money_sql(sql: str) -> bool:
    s = sql.lower()
    return "invoices" in s and bool(_SUM_AMOUNT.search(s)) and "paid" not in s


def quirk_handling(inv: Investigation, story: dict) -> dict:
    tables = [t for sig in story.get("match", []) for t in sig.get("quirk_tables", [])]
    flagged_l6 = any(w.get("kind") == "fanout_risk" and r.sql and any(t in r.sql.lower() for t in tables)
                     for r in inv.cut_results for w in r.warnings)
    flagged_skeptic = any(s.kind == "fanout_or_bug" and s.outcome == "failed" for f in inv.findings for s in f.skeptic)
    flagged_memory = any(any(t in q["text"].lower() for t in tables) or "fan-out" in q["text"].lower()
                         for q in build_delta(inv)["data_quirks"])
    carried = any(f.label == "confirmed" and any(_naive_money_sql(s) for s in f.sql) for f in inv.findings)
    carried |= any(f.label == "confirmed" and any(r.warnings for r in inv.cut_results
                                                   if r.sql in f.sql) for f in inv.findings)
    return {"flagged": flagged_l6 or flagged_skeptic or flagged_memory, "flagged_l6": flagged_l6,
            "flagged_skeptic": flagged_skeptic, "flagged_memory": flagged_memory, "carried_inflation": carried}


def _within(v: Any, truth: dict) -> bool:
    try:
        x, obs = float(v), float(truth["observed"])
    except (TypeError, ValueError):
        return False
    if math.isnan(x):
        return False
    if "tolerance_abs" in truth:
        return abs(x - obs) <= float(truth["tolerance_abs"]) + 1e-12
    return abs(x - obs) <= float(truth.get("tolerance_rel", 0.1)) * abs(obs)


# ── one investigation ────────────────────────────────────────────────────────

def score_investigation(inv: Investigation, key: dict | None = None, entry: dict | None = None) -> dict:
    key = key or load_key(inv.dataset)
    entry = entry or question_entry(inv.dataset, inv.question) or {"id": "?", "relevant": []}
    stories = {s["id"]: s for s in key["stories"]}
    relevant = [sid for sid in entry.get("relevant", []) if stories.get(sid, {}).get("kind") in RECALL_KINDS]

    per_finding = []
    found: dict[str, set[str]] = {"confirmed": set(), "any": set()}
    false_findings, unkeyed = [], []
    decoy_labels: list[str] = []
    for f in inv.findings:
        hits = classify(f, key)
        per_finding.append({"id": f.id, "label": f.label, "matches": hits, "statement": f.statement})
        for sid, how in hits:
            kind = stories[sid]["kind"]
            if kind == "decoy":
                decoy_labels.append(f.label)
            if f.label == "confirmed" and (how == "naive" or kind in ("decoy", "absent")):
                false_findings.append({"finding": f.id, "story": sid, "why": "naive trap" if how == "naive" else kind})
            if how == "correct" and kind in RECALL_KINDS:
                if f.label == "confirmed":
                    found["confirmed"].add(sid)
                if f.label in ("confirmed", "suggestive"):
                    found["any"].add(sid)
        if f.label == "confirmed" and not hits:
            unkeyed.append(f.id)

    def recall(s: set[str]) -> float | None:
        return round(len(s & set(relevant)) / len(relevant), 4) if relevant else None

    decoy = "pass"
    if "confirmed" in decoy_labels:
        decoy = "fail"
    elif "suggestive" in decoy_labels:
        decoy = "partial"

    trap = {}
    for s in key["stories"]:
        if s["kind"] != "trap":
            continue
        ids = {f["id"]: f for f in per_finding}
        corr = any((s["id"], "correct") in ids[f.id]["matches"] and f.label == "confirmed" for f in inv.findings)
        naive = any((s["id"], "naive") in ids[f.id]["matches"] and f.label == "confirmed" for f in inv.findings)
        trap[s["id"]] = "correct" if corr and not naive else ("naive_confirmed" if naive else "neither")
    quirks = {s["id"]: quirk_handling(inv, s) for s in key["stories"] if s["kind"] == "quirk"}

    nums = {"checked": 0, "within": 0}
    for f in inv.findings:
        for sid, how in classify(f, key):
            if how != "correct":
                continue
            for t in stories[sid].get("truths") or []:
                if t.get("observed") is None:
                    continue
                nums["checked"] += 1
                if any(_within(c.value, t) for c in f.evidence):
                    nums["within"] += 1

    md = render(inv)
    untraced = untraced_numbers(md, inv)
    statuses: dict[str, int] = {}
    for r in inv.cut_results:
        statuses[r.status] = statuses.get(r.status, 0) + 1
    test_verdicts = [v for v in inv.verdicts]
    untestable = [v for v in test_verdicts if v.label == "untestable"]
    reported = [f for f in inv.findings if f.label in ("confirmed", "suggestive")]
    matched = [f for f in reported if classify(f, key)]
    bonus = sorted(found["confirmed"] - set(relevant))
    return {
        "run_id": inv.run_id, "question_id": entry.get("id"), "question": inv.question, "config": inv.config,
        "dataset": inv.dataset, "mode": inv.mode,
        "recall_confirmed": recall(found["confirmed"]), "recall_any": recall(found["any"]),
        "found_confirmed": sorted(found["confirmed"]), "found_any": sorted(found["any"]), "relevant": relevant,
        "false_findings": false_findings, "n_false_findings": len(false_findings),
        "unkeyed_confirmed": unkeyed, "bonus": bonus,
        "decoy": decoy, "traps": trap, "quirks": quirks,
        "number_correctness": round(nums["within"] / nums["checked"], 4) if nums["checked"] else None,
        "traceability": 1.0 if not untraced else 0.0, "untraced_numbers": untraced,
        "pipeline": {"cuts": len(inv.cut_results), "status": statuses,
                     "untestable_share": round(len(untestable) / len(test_verdicts), 4) if test_verdicts else None,
                     "untestable_reasons": [v.reason[:120] for v in untestable],
                     "flagged_share": round(sum(1 for r in inv.cut_results if r.trust_badge == "flagged")
                                            / len(inv.cut_results), 4) if inv.cut_results else None,
                     "mean_attempts": round(sum(r.attempts for r in inv.cut_results) / len(inv.cut_results), 3)
                     if inv.cut_results else None},
        "cost": {k: inv.usage.get(k) for k in ("calls", "input_tokens", "output_tokens", "usd", "queries", "wall_ms")},
        "match_coverage": round(len(matched) / len(reported), 4) if reported else None,
        "findings": per_finding, "error": inv.error, "stopped_reason": inv.stopped_reason,
    }


# ── many investigations ──────────────────────────────────────────────────────

def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _range(xs):
    xs = [x for x in xs if x is not None]
    return [min(xs), max(xs)] if xs else None


def jaccard(sets: list[set]) -> float | None:
    if len(sets) < 2:
        return None
    vals = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            u = sets[i] | sets[j]
            vals.append(len(sets[i] & sets[j]) / len(u) if u else 1.0)
    return round(sum(vals) / len(vals), 4)


def aggregate(scores: list[dict]) -> dict:
    """Group by (config, question) for stability; then by config for headlines."""
    by_cq: dict[tuple, list[dict]] = {}
    for s in scores:
        by_cq.setdefault((s["config"], s["question_id"]), []).append(s)
    cells = []
    for (cfg, qid), ss in sorted(by_cq.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        cells.append({"config": cfg, "question_id": qid, "reps": len(ss),
                      "recall_confirmed": _mean([x["recall_confirmed"] for x in ss]),
                      "recall_confirmed_range": _range([x["recall_confirmed"] for x in ss]),
                      "false_findings": _mean([x["n_false_findings"] for x in ss]),
                      "false_findings_range": _range([x["n_false_findings"] for x in ss]),
                      "jaccard_found": jaccard([set(x["found_confirmed"]) for x in ss])})
    by_cfg: dict[str, list[dict]] = {}
    for s in scores:
        by_cfg.setdefault(s["config"], []).append(s)
    configs = {}
    for cfg, ss in sorted(by_cfg.items()):
        configs[cfg] = {
            "runs": len(ss),
            "recall_confirmed": _mean([x["recall_confirmed"] for x in ss]),
            "recall_any": _mean([x["recall_any"] for x in ss]),
            "stories_confirmed_per_question": _mean([len(set(x["found_confirmed"]) & set(x["relevant"])) for x in ss]),
            "false_findings_per_run": _mean([x["n_false_findings"] for x in ss]),
            "decoy_confirmed_runs": sum(1 for x in ss if x["decoy"] == "fail"),
            "traceability": _mean([x["traceability"] for x in ss]),
            "number_correctness": _mean([x["number_correctness"] for x in ss]),
            "untestable_share": _mean([x["pipeline"]["untestable_share"] for x in ss]),
            "match_coverage": _mean([x["match_coverage"] for x in ss]),
            "usd_per_run": _mean([x["cost"]["usd"] for x in ss]),
            "wall_s_per_run": _mean([(x["cost"]["wall_ms"] or 0) / 1000 for x in ss]),
        }
    return {"by_config": configs, "by_config_question": cells}


def format_table(agg: dict) -> str:
    cols = ["runs", "recall_confirmed", "recall_any", "stories_confirmed_per_question", "false_findings_per_run",
            "decoy_confirmed_runs", "untestable_share", "match_coverage", "usd_per_run", "wall_s_per_run"]
    lines = ["| config | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for cfg, m in agg["by_config"].items():
        lines.append(f"| {cfg} | " + " | ".join("-" if m[c] is None else str(m[c]) for c in cols) + " |")
    return "\n".join(lines)
