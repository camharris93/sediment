"""Data shapes for `investigate` (docs/investigate.md §4).

Everything here round-trips through JSON (`to_dict` / `from_dict`) so a recorded
investigation can be re-judged, re-skepticked and re-scored offline from its SQL.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

TestKind = Literal["group_diff", "before_after", "threshold", "reconcile"]
TEST_KINDS = ("group_diff", "before_after", "threshold", "reconcile")
Label = Literal["confirmed", "suggestive", "refuted", "untestable"]
LABELS = ("confirmed", "suggestive", "refuted", "untestable")
Direction = Literal["higher", "lower", "no_difference"]
DIRECTIONS = ("higher", "lower", "no_difference")
SKEPTIC_KINDS = ("small_n", "mix_shift", "simpsons", "definition_change", "fanout_or_bug")

# A closed metric vocabulary keeps Claims matchable without an LLM. It is generic
# analytics vocabulary, not tied to any dataset.
METRICS = ("logo_churn_rate", "revenue_churn_rate", "early_churn_rate", "retention_rate",
           "failed_payment_share", "new_account_share", "conversion_rate", "mrr", "revenue",
           "usage", "count", "other")

# `b` may name a real group value or REST: every row whose group is not `a`.
REST = "__rest__"


def _from(cls, d: dict | None):
    if d is None:
        return None
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class TestSpec:
    """A deterministic test evaluated in Python over a cut's rows."""
    kind: TestKind
    value_col: str
    n_col: str | None = None
    group_col: str | None = None
    a: Any = None
    b: Any = None
    time_col: str | None = None
    split_at: str | None = None
    direction: Literal["higher", "lower"] = "higher"
    min_rel_diff: float | None = None      # None -> JudgeConfig default
    min_n: int | None = None               # None -> JudgeConfig default
    part_col: str | None = None
    total_col: str | None = None

    __test__ = False  # not a pytest class

    @classmethod
    def from_dict(cls, d: dict) -> TestSpec:
        return _from(cls, d)


@dataclass
class Cut:
    id: str
    hypothesis_id: str
    purpose: Literal["test", "skeptic"]
    question: str
    expected_columns: list[str]
    grain: str
    test: TestSpec
    sql: str | None = None                 # set only for replay / hand-authored plans

    @classmethod
    def from_dict(cls, d: dict) -> Cut:
        c = _from(cls, d)
        c.test = TestSpec.from_dict(d["test"])
        return c


@dataclass
class Claim:
    metric: str
    dimensions: list[str]
    segment: dict[str, Any]
    direction: Direction
    window: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Claim:
        return _from(cls, d)


@dataclass
class Hypothesis:
    id: str
    statement: str
    mechanism: str
    claim: Claim
    cuts: list[Cut]
    known_from: str | None = None          # a memory finding id this restates

    @classmethod
    def from_dict(cls, d: dict) -> Hypothesis:
        h = _from(cls, d)
        h.claim = Claim.from_dict(d["claim"])
        h.cuts = [Cut.from_dict(c) for c in d["cuts"]]
        return h


@dataclass
class EvidenceCell:
    """The ONLY way a number reaches the report. `row_key` identifies the row it
    came from; a `derived` row_key marks a value Python computed from other cells
    (e.g. a relative difference) — still deterministic, never from an LLM."""
    cut_id: str
    column: str
    row_key: dict[str, Any]
    value: float | int | str

    @classmethod
    def from_dict(cls, d: dict) -> EvidenceCell:
        return _from(cls, d)


@dataclass
class CutResult:
    cut_id: str
    status: str
    sql: str | None
    rows: list[dict]
    trust_badge: str
    warnings: list[dict] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    truncated: bool = False
    attempts: int = 0
    elapsed_ms: int = 0
    usage: dict = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> CutResult:
        return _from(cls, d)


@dataclass
class Verdict:
    cut_id: str
    label: Label
    reason: str
    evidence: list[EvidenceCell] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)   # rel, z, n per arm (information)

    @classmethod
    def from_dict(cls, d: dict) -> Verdict:
        v = _from(cls, d)
        v.evidence = [EvidenceCell.from_dict(e) for e in d.get("evidence", [])]
        return v


@dataclass
class SkepticCheck:
    kind: str
    target_cut_id: str
    cut_ids: list[str]
    outcome: Literal["passed", "failed", "untestable"]
    reason: str
    confounder: str | None = None
    alt_definition: str | None = None
    evidence: list[EvidenceCell] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> SkepticCheck:
        s = _from(cls, d)
        s.evidence = [EvidenceCell.from_dict(e) for e in d.get("evidence", [])]
        return s


@dataclass
class Finding:
    id: str
    hypothesis_id: str
    label: Label
    statement: str
    claim: Claim
    evidence: list[EvidenceCell]
    sql: list[str]
    skeptic: list[SkepticCheck] = field(default_factory=list)
    skeptic_outcome: Literal["survived", "downgraded", "refuted", "not_run"] = "not_run"
    judged_label: Label | None = None      # the label before the skeptic touched it
    note: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Finding:
        f = _from(cls, d)
        f.claim = Claim.from_dict(d["claim"])
        f.evidence = [EvidenceCell.from_dict(e) for e in d.get("evidence", [])]
        f.skeptic = [SkepticCheck.from_dict(s) for s in d.get("skeptic", [])]
        return f


@dataclass
class Investigation:
    run_id: str
    question: str
    dataset: str
    config: str
    hypotheses: list[Hypothesis] = field(default_factory=list)
    cut_results: list[CutResult] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    skeptic_cuts: list[Cut] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    stopped_reason: str | None = None
    memory_in: str | None = None
    mode: str = "live"                      # live | replay | baseline
    error: str | None = None
    known: list[dict] = field(default_factory=list)   # memory findings not re-tested

    def to_dict(self) -> dict:
        return asdict(self)

    def result(self, cut_id: str) -> CutResult | None:
        return next((r for r in self.cut_results if r.cut_id == cut_id), None)

    def all_cuts(self) -> list[Cut]:
        return [c for h in self.hypotheses for c in h.cuts] + list(self.skeptic_cuts)

    @classmethod
    def from_dict(cls, d: dict) -> Investigation:
        inv = _from(cls, d)
        inv.hypotheses = [Hypothesis.from_dict(h) for h in d.get("hypotheses", [])]
        inv.cut_results = [CutResult.from_dict(r) for r in d.get("cut_results", [])]
        inv.verdicts = [Verdict.from_dict(v) for v in d.get("verdicts", [])]
        inv.skeptic_cuts = [Cut.from_dict(c) for c in d.get("skeptic_cuts", [])]
        inv.findings = [Finding.from_dict(f) for f in d.get("findings", [])]
        return inv
