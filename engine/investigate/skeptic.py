"""The skeptic (docs/investigate.md §5.4). Runs on CONFIRMED findings only, at most
two checks each. One LLM call picks checks from a fixed menu (and names a
confounder column or an alternative definition); each check that needs data is a
new Cut through the same pipeline; deterministic functions decide pass/fail.

The skeptic can only LOWER a label, never raise one.
"""
from __future__ import annotations

import json
from typing import Any

from ..query.grounding import GroundingContext, to_prompt_summary
from .evaluate import (
    DEFAULT_JUDGE,
    JudgeConfig,
    aggregate_arm,
    evaluate,
    norm,
    rel_diff,
    resolve_columns,
    split_arms,
    to_float,
)
from .memory import Memory
from .numfmt import fmt, pct
from .plan import known_columns, strip_json
from .types import SKEPTIC_KINDS, Cut, CutResult, EvidenceCell, Finding, Hypothesis, SkepticCheck

MAX_CHECKS = 2

SKEPTIC_SYSTEM = """\
You are the SKEPTIC layer of an analytics investigation harness. A finding has
been confirmed by a deterministic test. Your job is to choose the checks most
likely to EXPLAIN IT AWAY. You do not write SQL, compute numbers, or decide
whether a check passes — deterministic code does that.

Check menu (choose at most {max_checks}, most damaging first):
  - small_n: the effect rests on too few observations or one small group.
  - mix_shift: the comparison groups differ in composition on another column
    (a confounder); the effect may disappear when that mix is held constant.
    Name the confounder column.
  - simpsons: the effect may reverse within strata of a confounder.
    Name the confounder column.
  - definition_change: the effect may depend on how the metric is defined.
    Give an alternative definition in one sentence (prefer one from memory).
  - fanout_or_bug: the number may be inflated by a one-to-many join, duplicate
    rows, or counting incomplete transactions.

A confounder must be a real column from the schema that is NOT already one of
the finding's dimensions.

Return ONLY JSON:
{{"checks": [{{"kind": "simpsons", "target_cut_id": "<one of the evidence cut ids>",
              "confounder": "<column or null>", "alt_definition": "<text or null>",
              "rationale": "<one sentence>"}}]}}

# Schema
{schema}
"""


def skeptic_system(ctx: GroundingContext) -> str:
    return SKEPTIC_SYSTEM.format(max_checks=MAX_CHECKS, schema=to_prompt_summary(ctx))


def skeptic_user(finding: Finding, hyp: Hypothesis, results: dict[str, CutResult], memory: Memory | None) -> str:
    lines = [f"Finding ({finding.label}): {finding.statement}",
             f"Claim: {json.dumps({'metric': finding.claim.metric, 'dimensions': finding.claim.dimensions, 'segment': finding.claim.segment, 'direction': finding.claim.direction})}",
             "Evidence cuts:"]
    for c in hyp.cuts:
        r = results.get(c.id)
        lines.append(f"  - {c.id}: {c.question} (columns: {', '.join(c.expected_columns)}; test: {c.test.kind})")
        if r and r.sql:
            lines.append(f"    SQL: {' '.join(r.sql.split())[:600]}")
        if r and r.warnings:
            lines.append(f"    L6 warnings: {[w.get('kind') for w in r.warnings]}")
    if memory and memory.definitions:
        lines.append("Definitions in memory:")
        lines += [f"  - {d['id']}: {d['text']}" for d in memory.definitions]
    return "\n".join(lines)


def validate_checks(payload: Any, finding: Finding, hyp: Hypothesis, ctx: GroundingContext) -> list[dict]:
    """Keep only well-formed checks; drop (don't repair) the rest."""
    cols = known_columns(ctx)
    cut_ids = {c.id for c in hyp.cuts}
    dims = {d.lower() for d in finding.claim.dimensions}
    out: list[dict] = []
    for c in (payload or {}).get("checks", []) if isinstance(payload, dict) else []:
        if not isinstance(c, dict) or c.get("kind") not in SKEPTIC_KINDS:
            continue
        target = c.get("target_cut_id") if c.get("target_cut_id") in cut_ids else hyp.cuts[0].id
        conf = c.get("confounder")
        if c["kind"] in ("mix_shift", "simpsons"):
            if not conf or str(conf).lower() not in cols or str(conf).lower() in dims:
                continue
        alt = c.get("alt_definition")
        if c["kind"] == "definition_change" and not (alt and str(alt).strip()):
            continue
        out.append({"kind": c["kind"], "target_cut_id": target, "confounder": conf,
                    "alt_definition": (str(alt).strip()[:300] if alt else None)})
        if len(out) == MAX_CHECKS:
            break
    return out


def choose_checks(finding: Finding, hyp: Hypothesis, results: dict[str, CutResult], ctx: GroundingContext,
                  memory: Memory | None, *, model: str | None = None) -> list[dict]:
    from .._llm import complete
    from ..config import get_ai_settings

    text, _ = complete(system=skeptic_system(ctx), user=skeptic_user(finding, hyp, results, memory),
                       model=model or get_ai_settings().model_l1, max_tokens=1200)
    try:
        payload = json.loads(strip_json(text), strict=False)
    except json.JSONDecodeError:
        payload = None
    return validate_checks(payload, finding, hyp, ctx)


# ── skeptic cuts are built DETERMINISTICALLY from the target cut ─────────────

_DEDUP = (" Count each entity once: deduplicate any one-to-many join before aggregating, and include "
          "only completed (settled) transactions in any money amount.")


def build_skeptic_cut(check: dict, target: Cut, sid: str) -> Cut | None:
    k, conf, alt = check["kind"], check.get("confounder"), check.get("alt_definition")
    if k == "small_n":
        return None
    if k in ("simpsons", "mix_shift"):
        extra = [conf] if conf not in target.expected_columns else []
        return Cut(sid, target.hypothesis_id, "skeptic",
                   target.question + f" Additionally break the result down by {conf}.",
                   target.expected_columns + extra, f"{target.grain} per {conf}", target.test)
    if k == "definition_change":
        return Cut(sid, target.hypothesis_id, "skeptic",
                   target.question + f" Use this alternative definition of the metric instead: {alt}",
                   list(target.expected_columns), target.grain, target.test)
    if k == "fanout_or_bug":
        return Cut(sid, target.hypothesis_id, "skeptic", target.question + _DEDUP,
                   list(target.expected_columns), target.grain, target.test)
    return None


# ── deterministic check functions ────────────────────────────────────────────

def _arms(cut: Cut, r: CutResult):
    cols, missing = resolve_columns(r.rows, [c for c in [cut.test.value_col, cut.test.n_col,
                                                          cut.test.group_col, cut.test.time_col] if c])
    if missing:
        return None, f"missing columns {missing}"
    arms = split_arms(cut.id, cut.test, r.rows, cols)
    return (arms, cols) if not isinstance(arms, str) else (None, arms)


def _usable(r: CutResult | None) -> str | None:
    if r is None:
        return "check cut was not run"
    if r.status != "executed" or r.trust_badge in ("refused", "failed"):
        return f"check cut {r.status}/{r.trust_badge}" + (f": {r.error}" if r.error else "")
    if r.truncated:
        return "check cut truncated"
    if not r.rows:
        return "check cut returned no rows"
    return None


def check_small_n(target: Cut, r: CutResult, cfg: JudgeConfig) -> tuple[str, str, list[EvidenceCell]]:
    got, cols = _arms(target, r)
    if got is None:
        return "untestable", f"could not split arms: {cols}", []
    a, b = got
    min_n = target.test.min_n or cfg.min_n
    if a.n is None or b.n is None:
        return "failed", "no denominator column, so sample size cannot be shown to be adequate", a.cells + b.cells
    if a.n < min_n or b.n < min_n:
        return "failed", f"n={fmt(a.n)}/{fmt(b.n)} under {min_n}", a.cells + b.cells
    rates = all(v is not None and 0 <= v <= 1 for v in (a.value, b.value))
    if rates:
        ev_a, ev_b = a.value * a.n, b.value * b.n
        cells = a.cells + b.cells + [
            EvidenceCell(target.id, "events", {"arm": a.label, "derived": "value x n"}, round(ev_a, 2)),
            EvidenceCell(target.id, "events", {"arm": b.label, "derived": "value x n"}, round(ev_b, 2))]
        if min(ev_a, ev_b) < cfg.min_events:
            return "failed", (f"too few events behind the rates ({fmt(round(ev_a, 2))} vs "
                              f"{fmt(round(ev_b, 2))}; need >= {cfg.min_events} per arm)"), cells
    else:
        cells = a.cells + b.cells
    # Per-group cuts: the effect must survive dropping the smallest contributing row.
    nc = cols.get(target.test.n_col) if target.test.n_col else None
    pool = [("a", x) for x in a.rows] + [("b", x) for x in b.rows]
    if nc and len(pool) > 2 and (len(a.rows) > 1 or len(b.rows) > 1):
        drop_side, drop_row = min(pool, key=lambda p: to_float(p[1].get(nc)) or 0)
        ra = [x for x in a.rows if not (drop_side == "a" and x is drop_row)]
        rb = [x for x in b.rows if not (drop_side == "b" and x is drop_row)]
        if ra and rb:
            vc = cols[target.test.value_col]
            a2 = aggregate_arm(target.id, a.label, ra, vc, nc, {})
            b2 = aggregate_arm(target.id, b.label, rb, vc, nc, {})
            rel2 = rel_diff(a2.value, b2.value)
            want = 1 if target.test.direction == "higher" else -1
            cells.append(EvidenceCell(target.id, "rel_diff(without smallest group)",
                                      {"derived": "dropping the smallest-n row"}, round(rel2, 6)))
            if rel2 * want < (target.test.min_rel_diff or cfg.min_rel_diff):
                return "failed", f"effect does not survive dropping the smallest group (now {pct(rel2)})", cells
    return "passed", f"n={fmt(a.n)}/{fmt(b.n)}, enough events, robust to dropping the smallest group", cells


def _strata(target: Cut, r: CutResult, confounder: str):
    cols, missing = resolve_columns(r.rows, [c for c in [target.test.value_col, target.test.n_col,
                                                          target.test.group_col, target.test.time_col,
                                                          confounder] if c])
    if missing:
        return None, f"missing columns {missing}"
    cc = cols[confounder]
    out = []
    for s in sorted({norm(x.get(cc)) for x in r.rows}):
        rows = [x for x in r.rows if norm(x.get(cc)) == s]
        arms = split_arms(target.id, target.test, rows, cols)
        if not isinstance(arms, str) and arms[0].value is not None and arms[1].value is not None:
            out.append((s, arms[0], arms[1]))
    return out, None


def check_simpsons(target: Cut, r: CutResult, confounder: str, cfg: JudgeConfig):
    strata, err = _strata(target, r, confounder)
    if strata is None:
        return "untestable", err, []
    if len(strata) < 2:
        return "untestable", f"fewer than 2 {confounder} strata with both arms present", []
    want = 1 if target.test.direction == "higher" else -1
    total = rev = 0.0
    cells: list[EvidenceCell] = []
    for s, a, b in strata:
        w = (a.n or 1) + (b.n or 1)
        total += w
        if (a.value - b.value) * want < 0:
            rev += w
        cells.append(EvidenceCell(r.cut_id, f"rel_diff within {confounder}", {confounder: s, "derived": "(a-b)/|b|"},
                                  round(rel_diff(a.value, b.value), 6)))
    share = rev / total if total else 0.0
    cells.append(EvidenceCell(r.cut_id, "reversed_share", {"derived": f"n share of {confounder} strata reversing"},
                              round(share, 6)))
    if share > cfg.simpsons_frac:
        return "failed", (f"direction REVERSES within {confounder} strata covering "
                          f"{pct(round(share, 6)).lstrip('+')} of n — the pooled effect is a composition artifact"), cells
    return "passed", f"direction holds within {confounder} strata ({pct(round(share, 6)).lstrip('+')} of n reverses)", cells


def check_mix_shift(target: Cut, r: CutResult, confounder: str, cfg: JudgeConfig):
    strata, err = _strata(target, r, confounder)
    if strata is None:
        return "untestable", err, []
    if not target.test.n_col:
        return "untestable", "mix standardization needs a denominator column", []
    common = [(s, a, b) for s, a, b in strata if a.n and b.n]
    if len(common) < 2:
        return "untestable", f"fewer than 2 {confounder} strata present in both arms", []
    na, nb = sum(a.n for _, a, _ in common), sum(b.n for _, _, b in common)
    blend_a = sum(a.value * a.n for _, a, _ in common) / na
    blend_b = sum(b.value * b.n for _, _, b in common) / nb
    std_a = sum(b.n / nb * a.value for _, a, b in common)      # a's rates at b's mix
    raw, std = blend_a - blend_b, std_a - blend_b
    cells = [EvidenceCell(r.cut_id, "raw_change", {"derived": "blended a - blended b"}, round(raw, 6)),
             EvidenceCell(r.cut_id, "mix_standardized_change", {"derived": f"a at b's {confounder} mix - b"},
                          round(std, 6))]
    if raw == 0:
        return "untestable", "no raw change to explain", cells
    kept = std / raw
    cells.append(EvidenceCell(r.cut_id, "share_of_change_kept", {"derived": "standardized / raw"}, round(kept, 6)))
    cells.append(EvidenceCell(r.cut_id, "share_of_change_removed", {"derived": "1 - kept"}, round(1 - kept, 6)))
    if kept < 1 - cfg.mix_fail_frac:
        return "failed", (f"holding {confounder} mix constant removes {pct(round(1 - kept, 6)).lstrip('+')} of "
                          "the change — it is largely a mix shift"), cells
    return "passed", f"{pct(round(kept, 6)).lstrip('+')} of the change remains with {confounder} mix held constant", cells


def check_definition_change(target: Cut, orig: CutResult, alt: CutResult, cfg: JudgeConfig, alt_cut: Cut):
    v1 = evaluate(alt_cut, alt, cfg)
    if v1.label == "untestable":
        return "untestable", f"alternative definition untestable: {v1.reason}", []
    rel1 = v1.stats.get("rel")
    want = 1 if target.test.direction == "higher" else -1
    min_rel = target.test.min_rel_diff or cfg.min_rel_diff
    cells = v1.evidence
    if rel1 is None:
        return ("passed" if v1.label == "confirmed" else "failed"), f"alternative definition: {v1.reason}", cells
    if rel1 * want < 0:
        return "failed", f"direction FLIPS under the alternative definition ({pct(round(rel1, 6))})", cells
    if abs(rel1) < min_rel / 2:
        return "failed", f"effect shrinks to {pct(round(rel1, 6))} under the alternative definition", cells
    return "passed", f"effect holds under the alternative definition ({pct(round(rel1, 6))})", cells


def check_fanout(target: Cut, orig: CutResult, dedup: CutResult | None, cfg: JudgeConfig):
    fan = [w for w in orig.warnings if w.get("kind") == "fanout_risk"]
    if fan:
        return "failed", "L6 flagged fan-out risk: " + fan[0].get("message", ""), []
    if dedup is None or _usable(dedup):
        return "untestable", _usable(dedup) or "no reconcile cut", []
    got0, _ = _arms(target, orig)
    got1, _ = _arms(target, dedup)
    if got0 is None or got1 is None:
        return "untestable", "could not compare arms between the original and deduplicated cut", []
    gaps = [abs(rel_diff(x1.value, x0.value)) for x0, x1 in zip(got0, got1)
            if x0.value is not None and x1.value is not None]
    if not gaps:
        return "untestable", "no comparable values", []
    gap = max(gaps)
    cells = [EvidenceCell(dedup.cut_id, "reconcile_gap", {"derived": "max |dedup - original| / |original|"},
                          round(gap, 6))]
    if gap > cfg.reconcile_tol:
        return "failed", f"values change by {pct(round(gap, 6)).lstrip('+')} when rows are deduplicated — double counting", cells
    return "passed", f"values reconcile with a deduplicated recount (gap {pct(round(gap, 6)).lstrip('+')})", cells


def run_check(check: dict, target: Cut, results: dict[str, CutResult], skeptic_cut: Cut | None,
              cfg: JudgeConfig = DEFAULT_JUDGE) -> SkepticCheck:
    k = check["kind"]
    orig = results.get(target.id)
    sres = results.get(skeptic_cut.id) if skeptic_cut else None
    ids = [skeptic_cut.id] if skeptic_cut else []
    sc = SkepticCheck(k, target.id, ids, "untestable", "", check.get("confounder"), check.get("alt_definition"))
    if orig is None or _usable(orig):
        sc.reason = "original cut unusable"
        return sc
    if k == "small_n":
        out = check_small_n(target, orig, cfg)
    elif k == "fanout_or_bug":
        out = check_fanout(target, orig, sres, cfg)
    elif _usable(sres):
        out = ("untestable", _usable(sres), [])
    elif k == "simpsons":
        out = check_simpsons(target, sres, check["confounder"], cfg)
    elif k == "mix_shift":
        out = check_mix_shift(target, sres, check["confounder"], cfg)
    elif k == "definition_change":
        out = check_definition_change(target, orig, sres, cfg, skeptic_cut)
    else:
        out = ("untestable", f"unknown check {k}", [])
    sc.outcome, sc.reason, sc.evidence = out
    return sc


def outcome_of(checks: list[SkepticCheck]) -> tuple[str, str | None]:
    """(skeptic_outcome, new label or None for unchanged)."""
    if not checks:
        return "not_run", None
    if any(c.outcome == "failed" and c.kind in ("fanout_or_bug", "simpsons") for c in checks):
        return "refuted", "refuted"
    if any(c.outcome == "failed" for c in checks):
        return "downgraded", "suggestive"
    if all(c.outcome == "untestable" for c in checks):
        return "not_run", None    # stays confirmed; the report notes "skeptic could not test"
    return "survived", None
