"""The judge — deterministic, NO LLM (docs/investigate.md §5.3).

`evaluate(cut, result)` turns one cut's rows into a Verdict; `combine(labels)`
turns a hypothesis's test-cut verdicts into its label. The thresholds live in one
JudgeConfig, frozen before the first eval run; changing them after seeing results
is a logged protocol deviation (RESULTS.md).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .types import REST, Cut, CutResult, EvidenceCell, Label, TestSpec, Verdict


@dataclass(frozen=True)
class JudgeConfig:
    min_rel_diff: float = 0.20     # effect size to call it confirmed
    min_n: int = 30                # per arm
    refute_rel: float = 0.05       # |effect| under this with enough n = refuted
    reconcile_tol: float = 0.01    # reconcile gap over this fails
    ok_badges: tuple[str, ...] = ("clean", "self_corrected")
    # skeptic-only
    min_events: int = 10           # small_n: a rate needs >= this many events per arm
    mix_fail_frac: float = 0.5     # mix_shift: fail if more than this share of the change disappears
    simpsons_frac: float = 0.5     # simpsons: fail if strata covering more than this share of n reverse


DEFAULT_JUDGE = JudgeConfig()


# ── shared with evals/harness.py ─────────────────────────────────────────────

def check_invariants(rows: list[dict], expect: dict | None) -> tuple[bool, str]:
    """Result-shape invariants (golden evals + cut column checks)."""
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


# ── value helpers ────────────────────────────────────────────────────────────

def norm(v: Any) -> str:
    """Group values compare as normalized strings: True == 'true', 2024 == '2024'."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    return str(v).strip().lower()


_LABEL_TOKENS = re.compile(r"[a-z0-9]+")


def label_key(v: Any) -> tuple[str, ...]:
    """Format-tolerant key for a group label: the sorted alphanumeric tokens.
    'Q2-2026', 'q2 2026' and '2026-Q2' share a key; '1-6' and '1-3 months' do not.
    Protocol deviation 2026-10-30 (see docs/investigate.md): the first live run lost
    3 of 9 cuts to label punctuation alone."""
    return tuple(sorted(_LABEL_TOKENS.findall(norm(v))))


def same_label(x: Any, y: Any) -> bool:
    return norm(x) == norm(y) or label_key(x) == label_key(y)


def to_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None if v is None else float(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def iso_period(v: Any) -> str | None:
    """A time value as a sortable ISO date string: dates, 'YYYY-MM', 'YYYY-Qn'."""
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    if v is None:
        return None
    s = str(v).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    up = s.upper().replace(" ", "").replace("-", "")
    if len(up) == 6 and up[4] == "Q" and up[:4].isdigit() and up[5] in "1234":
        return f"{up[:4]}-{3 * (int(up[5]) - 1) + 1:02d}-01"
    if len(s) == 7 and s[4] == "-" and s[:4].isdigit() and s[5:].isdigit():
        return s + "-01"
    if len(s) == 4 and s.isdigit():
        return s + "-01-01"
    return None


def resolve_columns(rows: list[dict], wanted: list[str]) -> tuple[dict[str, str], list[str]]:
    """Map expected column names to the actual (case-insensitive) result keys."""
    keys = list(rows[0].keys()) if rows else []
    by_lower = {k.lower(): k for k in keys}
    mapping, missing = {}, []
    for w in wanted:
        if not w:
            continue
        k = by_lower.get(w.lower())
        if k is None:
            missing.append(w)
        else:
            mapping[w] = k
    return mapping, missing


@dataclass
class Arm:
    label: str
    value: float | None
    n: float | None
    rows: list[dict] = field(default_factory=list)
    cells: list[EvidenceCell] = field(default_factory=list)


def aggregate_arm(cut_id: str, label: str, rows: list[dict], value_col: str, n_col: str | None,
                  key: dict[str, Any]) -> Arm:
    """One arm's value: the row's value, or the n-weighted mean over several rows."""
    vals = [(to_float(r.get(value_col)), to_float(r.get(n_col)) if n_col else None) for r in rows]
    vals = [(v, n) for v, n in vals if v is not None]
    if not vals:
        return Arm(label, None, None, rows)
    if n_col and all(n is not None for _, n in vals):
        n_tot = sum(n for _, n in vals)
        value = sum(v * n for v, n in vals) / n_tot if n_tot else sum(v for v, _ in vals) / len(vals)
    else:
        n_tot = None
        value = sum(v for v, _ in vals) / len(vals)
    if len(rows) == 1:
        rk = {k: (norm(v) if isinstance(v, (date, datetime)) else v) for k, v in key.items()}
        cells = [EvidenceCell(cut_id, value_col, rk, rows[0][value_col])]
        if n_col:
            cells.append(EvidenceCell(cut_id, n_col, rk, rows[0].get(n_col)))
    else:
        how = "n-weighted mean" if n_tot is not None else "mean"
        rk = {**key, "derived": f"{how} of {value_col} over {len(vals)} rows"}
        cells = [EvidenceCell(cut_id, value_col, rk, round(value, 6))]
        if n_tot is not None:
            cells.append(EvidenceCell(cut_id, n_col, {**key, "derived": f"sum of {n_col}"},
                                      int(n_tot) if float(n_tot).is_integer() else n_tot))
    return Arm(label, value, n_tot, rows, cells)


def two_prop_z(p1: float, n1: float, p2: float, n2: float) -> float | None:
    """Two-proportion z (pooled). Information only — it does not change labels."""
    if not (0 <= p1 <= 1 and 0 <= p2 <= 1) or n1 <= 0 or n2 <= 0:
        return None
    p = (p1 * n1 + p2 * n2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return None if se == 0 else round((p1 - p2) / se, 3)


def rel_diff(a: float, b: float) -> float:
    if b == 0:
        return 0.0 if a == 0 else math.copysign(float("inf"), a)
    return (a - b) / abs(b)


# ── the arms of a comparison test ────────────────────────────────────────────

def split_arms(cut_id: str, t: TestSpec, rows: list[dict], cols: dict[str, str]) -> tuple[Arm, Arm] | str:
    """(a-arm, b-arm) for group_diff / before_after, or a reason it can't be done."""
    vc, nc = cols[t.value_col], cols.get(t.n_col) if t.n_col else None
    if t.kind == "group_diff":
        gc = cols.get(t.group_col) if t.group_col else None
        if gc is None:
            return "group_diff needs group_col"
        a_rows = [r for r in rows if same_label(r.get(gc), t.a)]
        if norm(t.b) == REST:
            b_rows = [r for r in rows if not same_label(r.get(gc), t.a)]
        else:
            b_rows = [r for r in rows if same_label(r.get(gc), t.b)]
        if not a_rows or not b_rows:
            seen = sorted({norm(r.get(gc)) for r in rows})[:12]
            return f"no rows for {t.group_col}={t.a!r} vs {t.b!r} (values seen: {seen})"
        return (aggregate_arm(cut_id, str(t.a), a_rows, vc, nc, {t.group_col: t.a}),
                aggregate_arm(cut_id, str(t.b), b_rows, vc, nc, {t.group_col: t.b}))
    if t.kind == "before_after":
        tc = cols.get(t.time_col) if t.time_col else None
        if tc is None or not t.split_at:
            return "before_after needs time_col and split_at"
        if t.group_col and t.a is not None:   # optional segment filter
            gc = cols.get(t.group_col)
            rows = [r for r in rows if gc and same_label(r.get(gc), t.a)]
        split = iso_period(t.split_at)
        periods = [(iso_period(r.get(tc)), r) for r in rows]
        if any(p is None for p, _ in periods) or split is None:
            return f"could not read {t.time_col} / split_at={t.split_at!r} as dates"
        before = [r for p, r in periods if p < split]
        after = [r for p, r in periods if p >= split]
        if not before or not after:
            return f"no rows on both sides of {t.split_at}"
        seg = {t.group_col: t.a} if t.group_col and t.a is not None else {}
        return (aggregate_arm(cut_id, "after", after, vc, nc, {**seg, t.time_col: f">= {t.split_at}"}),
                aggregate_arm(cut_id, "before", before, vc, nc, {**seg, t.time_col: f"< {t.split_at}"}))
    return f"{t.kind} has no arms"


def _untestable(cut_id: str, reason: str) -> Verdict:
    return Verdict(cut_id, "untestable", reason)


def evaluate(cut: Cut, r: CutResult | None, cfg: JudgeConfig = DEFAULT_JUDGE) -> Verdict:
    t = cut.test
    if r is None:
        return _untestable(cut.id, "cut was not run")
    if r.status != "executed":
        return _untestable(cut.id, f"pipeline status {r.status}" + (f": {r.error}" if r.error else ""))
    if r.trust_badge in ("refused", "failed"):
        return _untestable(cut.id, f"trust badge {r.trust_badge}")
    if r.truncated:
        return _untestable(cut.id, "result truncated at the row cap — the cut did not aggregate")
    if not r.rows:
        return _untestable(cut.id, "no rows returned")
    needed = [c for c in [t.value_col, t.n_col, t.group_col, t.time_col, t.part_col, t.total_col] if c]
    cols, missing = resolve_columns(r.rows, list(dict.fromkeys(cut.expected_columns + needed)))
    if missing:
        return _untestable(cut.id, f"missing expected columns {missing} (got {sorted(r.rows[0])})")

    min_rel = t.min_rel_diff if t.min_rel_diff is not None else cfg.min_rel_diff
    min_n = t.min_n if t.min_n is not None else cfg.min_n
    badge_ok = r.trust_badge in cfg.ok_badges

    if t.kind == "reconcile":
        parts = [to_float(x.get(cols[t.part_col])) for x in r.rows]
        totals = {to_float(x.get(cols[t.total_col])) for x in r.rows}
        if None in parts or None in totals:
            return _untestable(cut.id, "non-numeric part/total")
        total = totals.pop() if len(totals) == 1 else sum(to_float(x.get(cols[t.total_col])) for x in r.rows)
        part = sum(parts)
        gap = abs(part - total) / abs(total) if total else (0.0 if part == 0 else float("inf"))
        ev = [EvidenceCell(cut.id, t.part_col, {"derived": f"sum of {t.part_col}"}, round(part, 6)),
              EvidenceCell(cut.id, t.total_col, {"derived": "total"}, round(total, 6)),
              EvidenceCell(cut.id, "gap", {"derived": f"|sum({t.part_col}) - {t.total_col}| / {t.total_col}"},
                           round(gap, 6))]
        label: Label = "confirmed" if gap <= cfg.reconcile_tol else "refuted"
        return Verdict(cut.id, label, f"reconcile gap {gap:.2%} ({'within' if gap <= cfg.reconcile_tol else 'over'} "
                                      f"{cfg.reconcile_tol:.0%})", ev, {"gap": gap})

    if t.kind == "threshold":
        rows = r.rows
        if t.group_col and t.a is not None:
            rows = [x for x in rows if same_label(x.get(cols[t.group_col]), t.a)]
        if not rows:
            return _untestable(cut.id, f"no rows for {t.group_col}={t.a!r}")
        arm = aggregate_arm(cut.id, "value", rows, cols[t.value_col], cols.get(t.n_col) if t.n_col else None,
                            {t.group_col: t.a} if t.group_col else {})
        bound = to_float(t.b)
        if arm.value is None or bound is None:
            return _untestable(cut.id, "threshold needs a numeric value and numeric bound b")
        met = arm.value >= bound if t.direction == "higher" else arm.value <= bound
        n_ok = arm.n is not None and arm.n >= min_n
        cmp = ">=" if t.direction == "higher" else "<="
        if not met:
            return Verdict(cut.id, "refuted", f"{t.value_col}={arm.value:.4g} is not {cmp} {bound:g}", arm.cells)
        if n_ok and badge_ok:
            return Verdict(cut.id, "confirmed", f"{t.value_col}={arm.value:.4g} {cmp} {bound:g}, n={arm.n:g}", arm.cells)
        why = "no n" if arm.n is None else (f"n={arm.n:g} < {min_n}" if not n_ok else f"badge {r.trust_badge}")
        return Verdict(cut.id, "suggestive", f"{t.value_col}={arm.value:.4g} {cmp} {bound:g} but {why}", arm.cells)

    arms = split_arms(cut.id, t, r.rows, cols)
    if isinstance(arms, str):
        return _untestable(cut.id, arms)
    a, b = arms
    if a.value is None or b.value is None:
        return _untestable(cut.id, f"non-numeric {t.value_col}")
    rel = rel_diff(a.value, b.value)
    z = two_prop_z(a.value, a.n, b.value, b.n) if a.n and b.n else None
    stats = {"a": a.value, "b": b.value, "n_a": a.n, "n_b": b.n, "rel": rel, "z": z}
    rel_cell = EvidenceCell(cut.id, f"rel_diff({t.value_col})",
                            {"derived": f"({a.label} - {b.label}) / |{b.label}|"},
                            round(rel, 6) if math.isfinite(rel) else str(rel))
    ev = a.cells + b.cells + [rel_cell]
    dir_ok = a.value > b.value if t.direction == "higher" else a.value < b.value
    n_known = a.n is not None and b.n is not None
    n_ok = n_known and a.n >= min_n and b.n >= min_n
    desc = f"{a.label}={a.value:.4g} vs {b.label}={b.value:.4g} ({rel:+.1%})"
    if not dir_ok and a.value != b.value:
        return Verdict(cut.id, "refuted", f"opposite direction: {desc}", ev, stats)
    if abs(rel) < cfg.refute_rel and n_ok:
        return Verdict(cut.id, "refuted", f"no meaningful difference: {desc}", ev, stats)
    if abs(rel) >= min_rel and n_ok and badge_ok:
        return Verdict(cut.id, "confirmed", f"{desc}, n={a.n:g}/{b.n:g}", ev, stats)
    why = []
    if abs(rel) < min_rel:
        why.append(f"effect under {min_rel:.0%}")
    if not n_known:
        why.append("no denominator column")
    elif not n_ok:
        why.append(f"n={a.n:g}/{b.n:g} under {min_n}")
    if not badge_ok:
        why.append(f"badge {r.trust_badge}")
    return Verdict(cut.id, "suggestive", f"{desc}; " + ", ".join(why), ev, stats)


def combine(labels: list[Label]) -> Label:
    """A hypothesis's label from its TEST cuts' labels."""
    if not labels or all(x == "untestable" for x in labels):
        return "untestable"
    conf, refu = "confirmed" in labels, "refuted" in labels
    if conf and not refu:
        return "confirmed"
    if refu and not conf:
        return "refuted"
    return "suggestive"
