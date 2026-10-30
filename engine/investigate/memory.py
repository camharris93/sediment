"""Findings memory: `datasets/<name>/findings.yml` (docs/investigate.md §5.5).

AI proposes, a human commits — the same pattern as `scaffold` (*.proposed.sql).
An investigation only ever writes `findings.proposed/<run_id>.yml`;
`findings accept` appends chosen entries to findings.yml. The file is
APPEND-ONLY: `accept` re-parses what it wrote and asserts every pre-existing
entry is unchanged, and the only way to "change" an entry is a new one that
`supersedes` it.

Memory is context, not evidence: a remembered finding must be re-run before it
can appear as confirmed in a new report.

    python -m engine.investigate.memory show --dataset saasco
    python -m engine.investigate.memory accept <run_id> --dataset saasco [--ids p.f1,p.q1] [--by cam]
"""
from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from ..config import DATASETS_DIR, validate_dataset_name

SECTIONS = ("definitions", "findings", "data_quirks", "open_questions", "runs")
_PREFIX = {"definitions": "def", "findings": "f", "data_quirks": "q", "open_questions": "o"}


def findings_path(dataset: str) -> Path:
    """`$SEDIMENT_FINDINGS_PATH` overrides (evals write their memory under runs/,
    so the dataset's real findings.yml is never touched by an eval)."""
    env = os.environ.get("SEDIMENT_FINDINGS_PATH")
    if env:
        return Path(env)
    return DATASETS_DIR / validate_dataset_name(dataset) / "findings.yml"


def proposed_dir(dataset: str) -> Path:
    return findings_path(dataset).parent / "findings.proposed"


def empty_memory_doc(dataset: str) -> dict:
    return {"version": 1, "dataset": dataset, **{s: [] for s in SECTIONS}}


@dataclass
class Memory:
    dataset: str
    doc: dict = field(default_factory=dict)
    sha: str | None = None

    @property
    def superseded(self) -> set[str]:
        return {e["supersedes"] for s in SECTIONS for e in self.doc.get(s, []) or []
                if isinstance(e, dict) and e.get("supersedes")}

    def _live(self, section: str) -> list[dict]:
        return [e for e in self.doc.get(section, []) or [] if e.get("id") not in self.superseded]

    @property
    def definitions(self) -> list[dict]:
        return [d for d in self._live("definitions") if d.get("status", "accepted") == "accepted"]

    @property
    def findings(self) -> list[dict]:
        return self._live("findings")

    @property
    def quirks(self) -> list[dict]:
        return self._live("data_quirks")

    @property
    def open_questions(self) -> list[dict]:
        return self._live("open_questions")

    def is_empty(self) -> bool:
        return not (self.definitions or self.findings or self.quirks or self.open_questions)

    def prompt_block(self) -> str:
        """What the planner and skeptic see. Only accepted, non-superseded entries."""
        if self.is_empty():
            return ""
        lines = ["# Memory from earlier investigations of this dataset (context, NOT evidence):"]
        if self.definitions:
            lines.append("Definitions in force:")
            lines += [f"  - {d['id']}: {d['text']}" for d in self.definitions]
        if self.findings:
            lines.append("Known findings (must be re-tested before they can be reported as confirmed):")
            lines += [f"  - {f['id']} [{f.get('label')}, skeptic {f.get('skeptic_outcome')}]: {f['statement']}"
                      for f in self.findings]
        if self.quirks:
            lines.append("Data quirks:")
            lines += [f"  - {q['id']}: {q['text']}" for q in self.quirks]
        if self.open_questions:
            lines.append("Open questions:")
            lines += [f"  - {o['id']}: {o['text']}" for o in self.open_questions]
        return "\n".join(lines)


def load_findings(dataset: str, path: Path | None = None) -> Memory:
    p = path or findings_path(dataset)
    if not p.exists():
        return Memory(dataset, empty_memory_doc(dataset), None)
    raw = p.read_bytes()
    doc = yaml.safe_load(raw) or {}
    for s in SECTIONS:
        doc.setdefault(s, [])
        doc[s] = doc[s] or []
    return Memory(dataset, doc, hashlib.sha256(raw).hexdigest()[:12])


# ── propose (an investigation's output) ──────────────────────────────────────

def build_delta(inv) -> dict:
    """The memory delta an investigation proposes. Never touches findings.yml."""
    from .types import Investigation
    assert isinstance(inv, Investigation)
    today = date.today().isoformat()
    findings, quirks, open_qs = [], [], []
    for f in inv.findings:
        if f.label not in ("confirmed", "suggestive"):
            continue
        findings.append({
            "id": f"p.f{len(findings) + 1}", "statement": f.statement,
            "claim": {"metric": f.claim.metric, "dimensions": list(f.claim.dimensions),
                      "segment": dict(f.claim.segment), "direction": f.claim.direction,
                      "window": f.claim.window},
            "label": f.label, "skeptic_outcome": f.skeptic_outcome, "evidence_sql": list(f.sql),
            "run_id": inv.run_id, "added": today, "supersedes": None})
    seen_quirks: set[str] = set()
    for r in inv.cut_results:
        for w in r.warnings:
            if w.get("kind") == "fanout_risk" and w["message"] not in seen_quirks:
                seen_quirks.add(w["message"])
                quirks.append({"id": f"p.q{len(quirks) + 1}", "text": w["message"], "source": f"L6 on {r.cut_id}",
                               "run_id": inv.run_id, "added": today})
    for f in inv.findings:
        for s in f.skeptic:
            if s.kind == "fanout_or_bug" and s.outcome == "failed" and s.reason not in seen_quirks:
                seen_quirks.add(s.reason)
                quirks.append({"id": f"p.q{len(quirks) + 1}", "text": s.reason,
                               "source": f"skeptic on {s.target_cut_id}", "run_id": inv.run_id, "added": today})
    recurring = Counter(a.strip() for r in inv.cut_results for a in set(r.assumptions) if a.strip())
    for text, n in recurring.most_common():
        if n >= 2 and text not in seen_quirks:
            seen_quirks.add(text)
            quirks.append({"id": f"p.q{len(quirks) + 1}", "text": f"Recurring assumption ({n} cuts): {text}",
                           "source": "L1 assumptions", "run_id": inv.run_id, "added": today})
    for f in inv.findings:
        if f.label == "untestable":
            open_qs.append({"id": f"p.o{len(open_qs) + 1}", "text": f"Untested: {f.statement}",
                            "reason": f.note, "run_id": inv.run_id, "added": today})
    return {"version": 1, "dataset": inv.dataset, "run_id": inv.run_id, "question": inv.question,
            "config": inv.config, "findings": findings, "data_quirks": quirks, "open_questions": open_qs}


def propose_delta(inv, out_dir: Path | None = None) -> Path:
    out = out_dir or proposed_dir(inv.dataset)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{inv.run_id}.yml"
    p.write_text("# PROPOSED memory delta — nothing here is in findings.yml until a human runs\n"
                 f"#   python run.py findings accept {inv.dataset} {inv.run_id} [--ids p.f1,p.q1]\n"
                 + yaml.safe_dump(build_delta(inv), sort_keys=False, allow_unicode=True), encoding="utf-8")
    return p


# ── accept (the human commit) ────────────────────────────────────────────────

class AppendOnlyViolation(RuntimeError):
    pass


def _next_id(doc: dict, section: str) -> str:
    prefix = _PREFIX[section]
    nums = [int(e["id"].split(".")[-1]) for e in doc.get(section, []) or []
            if str(e.get("id", "")).startswith(prefix + ".") and e["id"].split(".")[-1].isdigit()]
    return f"{prefix}.{(max(nums) if nums else 0) + 1:04d}"


def accept(dataset: str, run_id: str, *, ids: list[str] | None = None, by: str | None = None,
           auto: bool = False, path: Path | None = None, proposed: Path | None = None) -> list[str]:
    """Append chosen proposed entries to findings.yml with new sequential ids.

    `auto=True` is the eval-only policy (§8.4): definitions and data quirks, plus
    findings whose skeptic_outcome is `survived`, written as `eval-auto`."""
    target = path or findings_path(dataset)
    prop_path = proposed or (target.parent / "findings.proposed" / f"{run_id}.yml")
    if not prop_path.exists():
        raise FileNotFoundError(f"no proposed delta at {prop_path}")
    delta = yaml.safe_load(prop_path.read_text(encoding="utf-8")) or {}
    before = load_findings(dataset, target).doc
    snapshot = copy.deepcopy(before)
    doc = copy.deepcopy(before)
    who = "eval-auto" if auto else (by or os.environ.get("USER") or getpass.getuser() or "unknown")
    added: list[str] = []
    for section in ("definitions", "findings", "data_quirks", "open_questions"):
        for e in delta.get(section, []) or []:
            if ids is not None and e.get("id") not in ids:
                continue
            if auto and section == "open_questions":
                continue
            if auto and section == "findings" and e.get("skeptic_outcome") != "survived":
                continue
            entry = {k: v for k, v in e.items() if k != "id"}
            entry = {"id": _next_id(doc, section), **entry, "proposed_id": e.get("id"), "added_by": who}
            doc[section].append(entry)
            added.append(entry["id"])
    if added:
        doc["runs"].append({"run_id": run_id, "question": delta.get("question"),
                            "config": delta.get("config"), "accepted": added})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    assert_append_only(snapshot, load_findings(dataset, target).doc)
    return added


def assert_append_only(old: dict, new: dict) -> None:
    for s in SECTIONS:
        o, n = old.get(s, []) or [], new.get(s, []) or []
        if n[:len(o)] != o:
            raise AppendOnlyViolation(f"section {s!r}: existing entries were modified or removed")
    for k in ("version", "dataset"):
        if old.get(k) is not None and old.get(k) != new.get(k):
            raise AppendOnlyViolation(f"header {k!r} changed")


def _show(dataset: str) -> None:
    m = load_findings(dataset)
    p = findings_path(dataset)
    print(f"# {p} (sha {m.sha})" if m.sha else f"# {p} does not exist yet (empty memory)")
    print(yaml.safe_dump(m.doc, sort_keys=False, allow_unicode=True))
    props = sorted(proposed_dir(dataset).glob("*.yml")) if proposed_dir(dataset).exists() else []
    if props:
        print("# Proposed deltas awaiting review:")
        for pp in props:
            print(f"#   {pp.stem}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Findings memory (append-only).")
    ap.add_argument("command", choices=["show", "accept"])
    ap.add_argument("run_id", nargs="?")
    ap.add_argument("--dataset", default=os.environ.get("DATASET", "saasco"))
    ap.add_argument("--ids", help="comma-separated proposed ids (default: all)")
    ap.add_argument("--by", help="who is accepting (default: $USER)")
    a = ap.parse_args(argv)
    if a.command == "show":
        _show(a.dataset)
        return 0
    if not a.run_id:
        ap.error("accept needs a run_id")
    added = accept(a.dataset, a.run_id, ids=a.ids.split(",") if a.ids else None, by=a.by)
    print(f"[findings] appended {len(added)} entr{'y' if len(added) == 1 else 'ies'}: {', '.join(added) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
