"""Report rendering (docs/investigate.md §5.6). Template-rendered markdown — no LLM
prose. Every number in the Findings section is formatted from an EvidenceCell;
`untraced_numbers()` proves it (tests fail on any untraced numeral). Free text
from the planner (hypothesis statements) has any numerals masked, because a
number the model typed is not evidence.
"""
from __future__ import annotations

import json
import re
from dataclasses import astuple
from typing import Any

from .evaluate import DEFAULT_JUDGE, JudgeConfig
from .numfmt import fmt, pct
from .types import EvidenceCell, Finding, Investigation

_LABEL_ORDER = ("confirmed", "suggestive", "refuted", "untestable")
_LABEL_TITLE = {"confirmed": "Confirmed", "suggestive": "Suggestive", "refuted": "Refuted",
                "untestable": "Untestable"}
_SKEPTIC_MARK = {"passed": "passed", "failed": "FAILED", "untestable": "could not test"}
FINDINGS_START, FINDINGS_END = "<!-- findings:start -->", "<!-- findings:end -->"


_NUM = re.compile(r"(?<![\w.])[+-]?\d(?:[\d,]*\d)?(?:\.\d+)?%?")
_YEARISH = re.compile(r"^(19|20)\d\d$")


def mask_numerals(text: str, allowed: set[str] | None = None) -> str:
    """Keep years and traceable tokens; replace every other numeral with `#`.
    (Identifiers like Q2 or h1 are safe: the pattern skips digits glued to a word.)"""
    allowed = allowed or set()

    def sub(m: re.Match) -> str:
        tok = m.group(0)
        return tok if _YEARISH.match(tok) or tok in allowed or tok.lstrip("+") in allowed else "#"
    return _NUM.sub(sub, text)


def _cells_line(cells: list[EvidenceCell]) -> str:
    parts = []
    for c in cells:
        key = ", ".join(f"{k}={v}" for k, v in c.row_key.items() if k != "derived")
        tag = f" ({key})" if key else ""
        val = pct(c.value) if c.column.startswith("rel_diff") else fmt(c.value)
        parts.append(f"`{c.column}`{tag} = **{val}**")
    return "; ".join(parts)


def _finding_block(f: Finding, inv: Investigation, allowed: set[str]) -> list[str]:
    out = [f"#### {f.id} · {mask_numerals(f.statement)}", ""]
    seg = ", ".join(f"{k} = {v}" for k, v in f.claim.segment.items()) or "(none)"
    out.append(f"- **Claim:** `{f.claim.metric}` is **{f.claim.direction}** for {seg} "
               f"· dimensions: {', '.join(f'`{d}`' for d in f.claim.dimensions)}")
    if f.judged_label and f.judged_label != f.label:
        out.append(f"- **Judge said {f.judged_label}; skeptic {f.skeptic_outcome} it to {f.label}.**")
    for v in [x for x in inv.verdicts if x.cut_id.startswith(f.hypothesis_id + ".")]:
        r = inv.result(v.cut_id)
        badge = r.trust_badge if r else "-"
        out.append(f"- `{v.cut_id}` → **{v.label}** (badge `{badge}`): {_cells_line(v.evidence) or v.reason}")
        if v.label in ("untestable",):
            out.append(f"  - {mask_numerals(v.reason, allowed)}")
    for s in f.skeptic:
        line = f"- Skeptic `{s.kind}`"
        if s.confounder:
            line += f" on `{s.confounder}`"
        line += f" → **{_SKEPTIC_MARK[s.outcome]}**"
        if s.evidence:
            line += ": " + _cells_line(s.evidence)
        out.append(line)
        out.append(f"  - {mask_numerals(s.reason, allowed)}")
    if f.note:
        out.append(f"- _{f.note}_")
    if f.sql:
        out += ["", "<details><summary>SQL behind this finding</summary>", ""]
        for sql in f.sql:
            out += ["```sql", sql.strip(), "```", ""]
        out.append("</details>")
    out.append("")
    return out


def render(inv: Investigation, cfg: JudgeConfig = DEFAULT_JUDGE) -> str:
    allowed = allowed_tokens(inv, cfg)
    lines = [f"# Investigation: {inv.question}", "",
             f"`dataset` {inv.dataset} · `config` {inv.config} · `mode` {inv.mode} · `run` {inv.run_id}", ""]
    if inv.mode == "replay":
        lines += ["> **Replay:** cut SQL was supplied (recorded or hand-authored), not generated. It ran "
                  "through the deterministic L3-L6 layers, the judge and the skeptic check functions. "
                  "No LLM was called.", ""]
    lines += ["> Labels mean *the data is consistent with this* — not causation. Every number in the "
              "findings is an EvidenceCell from a result that passed L3-L5; no number was written by a model.", ""]
    if inv.error:
        lines += [f"**Investigation failed:** {inv.error}", ""]
    counts = {lab: sum(1 for f in inv.findings if f.label == lab) for lab in _LABEL_ORDER}
    lines += ["| confirmed | suggestive | refuted | untestable |", "|---|---|---|---|",
              "| " + " | ".join(str(counts[k]) for k in _LABEL_ORDER) + " |", ""]
    lines += ["## Findings", "", FINDINGS_START, ""]
    for lab in _LABEL_ORDER:
        fs = [f for f in inv.findings if f.label == lab]
        if not fs:
            continue
        lines += [f"### {_LABEL_TITLE[lab]}", ""]
        for f in fs:
            lines += _finding_block(f, inv, allowed)
    lines += [FINDINGS_END, ""]
    if inv.known:
        lines += ["## Known from memory, not re-tested", ""]
        lines += [f"- {k['id']}: {k['statement']}" for k in inv.known] + [""]
    lines += ["## What this didn't test", ""]
    gaps = [f for f in inv.findings if f.label == "untestable"]
    if inv.stopped_reason:
        lines.append(f"- Stopped early: **{inv.stopped_reason}**; remaining cuts were not run.")
    for f in gaps:
        lines.append(f"- {f.hypothesis_id}: {mask_numerals(f.statement)} — {f.note or 'untestable'}")
    if not gaps and not inv.stopped_reason:
        lines.append("- Every hypothesis produced a testable result.")
    lines.append("- Causation: none of these labels establish it.")
    lines += ["", "## Pipeline health", "", "| cut | purpose | status | badge | attempts | rows | L6 warnings |",
              "|---|---|---|---|---|---|---|"]
    purposes = {c.id: c.purpose for c in inv.all_cuts()}
    for r in inv.cut_results:
        warns = ", ".join(sorted({w.get("kind", "?") for w in r.warnings})) or "-"
        lines.append(f"| `{r.cut_id}` | {purposes.get(r.cut_id, '-')} | {r.status} | {r.trust_badge} | "
                     f"{r.attempts} | {len(r.rows)} | {warns} |")
    u = inv.usage or {}
    lines += ["", "## Cost", "",
              f"- LLM calls: {u.get('calls', 0)} · input tokens: {u.get('input_tokens', 0):,} · "
              f"output tokens: {u.get('output_tokens', 0):,} · est. USD: {u.get('usd', 0):.4f}",
              f"- Queries (cuts run): {u.get('queries', 0)} · wall clock: {u.get('wall_ms', 0) / 1000:.1f}s", ""]
    return "\n".join(lines)


def to_json(inv: Investigation) -> str:
    return json.dumps(inv.to_dict(), indent=2, default=str)


def allowed_tokens(inv: Investigation, cfg: JudgeConfig = DEFAULT_JUDGE) -> set[str]:
    """Formatted EvidenceCell values, plus the frozen JudgeConfig thresholds (a
    reason may cite 'under 30')."""
    values: list[Any] = [c.value for v in inv.verdicts for c in v.evidence]
    values += [c.value for f in inv.findings for s in f.skeptic for c in s.evidence]
    ok: set[str] = set()
    for v in values:
        for s in (fmt(v), pct(v)):
            ok.add(s)
            ok.add(s.lstrip("+"))
    for v in astuple(cfg):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            ok |= {fmt(v), f"{v:.0%}", str(v)}
    return ok


def untraced_numbers(md: str, inv: Investigation, cfg: JudgeConfig = DEFAULT_JUDGE) -> list[str]:
    """Numerals in the Findings section that do not come from an EvidenceCell.
    Ignores code/SQL blocks, backticked identifiers, years, quarters, and the
    row-key labels that name which row a cell came from."""
    if FINDINGS_START not in md:
        return []
    body = md.split(FINDINGS_START, 1)[1].split(FINDINGS_END, 1)[0]
    body = re.sub(r"<details>.*?</details>", " ", body, flags=re.S)
    body = re.sub(r"`[^`]*`", " ", body)
    body = re.sub(r"\(([^()]*=[^()]*)\)", " ", body)          # (group=value) row keys
    body = re.sub(r"\b\d{4}-\d{2}(-\d{2})?\b", " ", body)      # dates
    body = re.sub(r"^#### \S+", " ", body, flags=re.M)        # finding ids
    allowed = allowed_tokens(inv, cfg)
    bad = []
    for m in _NUM.finditer(body):
        tok = m.group(0)
        if _YEARISH.match(tok) or tok in allowed or tok.lstrip("+") in allowed:
            continue
        bad.append(tok)
    return bad
