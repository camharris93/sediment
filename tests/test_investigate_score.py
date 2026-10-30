"""Deterministic matching + scoring, and the reference plans end to end on the
real planted dataset (skipped when saasco isn't built)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from engine.investigate.types import Claim, Finding, Investigation
from evals.investigations.score import classify, load_key, load_questions, match_sig, score_investigation

ROOT = Path(__file__).resolve().parents[1]
PLANS = ROOT / "evals" / "investigations" / "reference_plans"


def claim(**kw) -> Claim:
    base = {"metric": "logo_churn_rate", "dimensions": [], "segment": {}, "direction": "higher"}
    return Claim(**{**base, **kw})


def test_match_grammar():
    sig = {"dimension_all": ["sales_assisted", "*size_band"], "segment": {"sales_assisted": True}, "direction": "lower"}
    assert match_sig(claim(dimensions=["sales_assisted", "company_size_band"], segment={"sales_assisted": "true"},
                           direction="lower"), sig)
    assert not match_sig(claim(dimensions=["sales_assisted"], segment={"sales_assisted": True}, direction="lower"), sig)
    assert match_sig(claim(dimensions=["derived:months_to_renewal"]), {"dimension_any": ["derived:*renew*"]})
    assert not match_sig(claim(metric="mrr"), {"metric_in": ["logo_churn_rate"]})


def test_trap_and_decoy_classification():
    key = load_key("saasco")
    naive = Finding("F1", "h1", "confirmed", "s", claim(dimensions=["sales_assisted"], segment={"sales_assisted": True}), [], [])
    decoy = Finding("F2", "h2", "confirmed", "s", claim(dimensions=["industry"], segment={"industry": "healthcare"}), [], [])
    assert classify(naive, key) == [("S5", "naive")]
    assert classify(decoy, key) == [("D1", "correct")]
    inv = Investigation("r", "Does having a salesperson involved help retention?", "saasco", "full", findings=[naive, decoy])
    sc = score_investigation(inv)
    assert sc["recall_confirmed"] == 0.0 and sc["n_false_findings"] == 2
    assert sc["decoy"] == "fail" and sc["traps"]["S5"] == "naive_confirmed"


def test_holdout_absent_story_counts_as_false():
    key = load_key("saasco_holdout")
    act = Finding("F1", "h1", "confirmed", "s", claim(dimensions=["first_8wk_peak_active_users"]), [], [])
    inv = Investigation("r", "What predicts which new accounts will churn in their first six months?",
                        "saasco_holdout", "full", findings=[act])
    sc = score_investigation(inv, key)
    assert sc["false_findings"] == [{"finding": "F1", "story": "S4", "why": "absent"}]


def test_questions_have_variant_overrides():
    dev = {q["id"]: q for q in load_questions("saasco")}
    ho = {q["id"]: q for q in load_questions("saasco_holdout")}
    assert "Europe" in dev["Q3"]["question"] and "Latin America" in ho["Q3"]["question"]


def _built(ds: str) -> bool:
    from evals.investigations.run import schemas_present
    return schemas_present(ds)


@pytest.mark.skipif(not _built("saasco"), reason="needs `python run.py up saasco`")
def test_truths_match_the_built_warehouse():
    from evals.investigations.run import run_truths
    bad = [(t, d) for t, ok, d in run_truths("saasco") if not ok]
    assert not bad


@pytest.mark.skipif(not _built("saasco"), reason="needs `python run.py up saasco`")
@pytest.mark.parametrize("plan, expect", [
    ("q4_sales_assist_dev.yaml", {"F1": ("confirmed", "refuted")}),
    ("q1_why_churn_up_dev.yaml", {"F1": ("confirmed", "confirmed"), "F3": ("confirmed", "suggestive"),
                                  "F4": ("suggestive", "suggestive")}),
    ("q3_europe_dev.yaml", {"F1": ("confirmed", "confirmed"), "F2": ("refuted", "refuted")}),
])
def test_reference_plans_replay_as_documented(plan, expect):
    from engine.investigate.report import render, untraced_numbers
    from engine.investigate.runner import replay
    from engine.query.grounding import build_grounding_context
    inv = replay(yaml.safe_load((PLANS / plan).read_text()), build_grounding_context("saasco"))
    got = {f.id: (f.judged_label, f.label) for f in inv.findings}
    for fid, labels in expect.items():
        assert got[fid] == labels, (fid, got)
    assert untraced_numbers(render(inv), inv) == []
    sc = score_investigation(inv)
    assert sc["n_false_findings"] == 0 and sc["traceability"] == 1.0
