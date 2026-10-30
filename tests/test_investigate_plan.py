"""Deterministic plan validation (no LLM)."""
from __future__ import annotations

import copy

from engine.investigate.budget import Budget
from engine.investigate.plan import planner_system, validate_plan

GOOD = {"hypotheses": [
    {"statement": "a", "mechanism": "m", "boring": True,
     "claim": {"metric": "logo_churn_rate", "dimensions": ["class", "derived:size_bucket"], "segment": {"class": "Aves"},
               "direction": "higher"},
     "cuts": [{"question": "q", "expected_columns": ["class", "n", "rate"], "grain": "class",
               "test": {"kind": "group_diff", "value_col": "rate", "n_col": "n", "group_col": "class",
                        "a": "Aves", "b": "__rest__", "direction": "higher"}}]},
    {"statement": "b", "mechanism": "m",
     "claim": {"metric": "count", "dimensions": ["species"], "segment": {}, "direction": "no_difference"},
     "cuts": [{"question": "q2", "expected_columns": ["period", "n", "v"], "grain": "period",
               "test": {"kind": "before_after", "value_col": "v", "n_col": "n", "time_col": "period",
                        "split_at": "2026-01-01", "direction": "higher"}}]}]}


def test_good_plan_validates_and_ids_are_assigned(grounding):
    hs, errs = validate_plan(GOOD, grounding, Budget())
    assert errs == [] and [h.id for h in hs] == ["h1", "h2"] and hs[1].cuts[0].id == "h2.c1"


def test_each_rule_is_enforced(grounding):
    cases = {
        "not in": lambda p: p["hypotheses"][0]["cuts"][0]["test"].update(value_col="missing"),
        "metric": lambda p: p["hypotheses"][0]["claim"].update(metric="vibes"),
        "derived:": lambda p: p["hypotheses"][0]["claim"].update(dimensions=["no_such_column"]),
        "boring": lambda p: p["hypotheses"][0].pop("boring"),
        "test.kind": lambda p: p["hypotheses"][0]["cuts"][0]["test"].update(kind="vibe_check"),
        "split_at": lambda p: p["hypotheses"][1]["cuts"][0]["test"].update(split_at="soon"),
        "1-3 cuts": lambda p: p["hypotheses"][1].update(cuts=[]),
    }
    for needle, mutate in cases.items():
        p = copy.deepcopy(GOOD)
        mutate(p)
        _, errs = validate_plan(p, grounding, Budget())
        assert any(needle in e for e in errs), (needle, errs)


def test_budget_limits_cuts(grounding):
    _, errs = validate_plan(GOOD, grounding, Budget(max_cuts=1))
    assert any("exceeds the budget" in e for e in errs)


def test_planner_prompt_carries_the_schema_and_budget(grounding):
    s = planner_system(grounding, Budget(max_hypotheses=4, max_cuts=12))
    assert "anage_marts.mart_longevity_by_class" in s and "at most 12 cuts" in s
