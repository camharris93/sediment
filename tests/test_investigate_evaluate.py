"""The judge: pure rows in, verdict out. No warehouse, no LLM."""
from __future__ import annotations

import pytest

from engine.investigate.evaluate import JudgeConfig, combine, evaluate, iso_period, two_prop_z
from engine.investigate.types import Cut, CutResult, TestSpec


def cut(test: TestSpec, cols: list[str]) -> Cut:
    return Cut("h1.c1", "h1", "test", "q", cols, "g", test)


def res(rows, badge="clean", status="executed", truncated=False) -> CutResult:
    return CutResult("h1.c1", status, "select 1", rows, badge, truncated=truncated)


GD = TestSpec("group_diff", "rate", n_col="n", group_col="seg", a="x", b="y", direction="higher")
COLS = ["seg", "n", "rate"]


def rows(ra, na, rb, nb):
    return [{"seg": "x", "n": na, "rate": ra}, {"seg": "y", "n": nb, "rate": rb}]


def test_confirmed_needs_direction_effect_n_and_a_clean_badge():
    v = evaluate(cut(GD, COLS), res(rows(0.30, 100, 0.20, 100)))
    assert v.label == "confirmed"
    assert v.stats["rel"] == pytest.approx(0.5)
    assert {c.column for c in v.evidence} >= {"rate", "n", "rel_diff(rate)"}


@pytest.mark.parametrize("r, why", [
    (rows(0.22, 100, 0.20, 100), "effect under"),      # +10%: below min_rel_diff
    (rows(0.30, 10, 0.20, 100), "under 30"),           # small n
])
def test_suggestive_when_effect_small_or_n_small(r, why):
    v = evaluate(cut(GD, COLS), res(r))
    assert v.label == "suggestive" and why in v.reason


def test_flagged_badge_caps_at_suggestive():
    assert evaluate(cut(GD, COLS), res(rows(0.30, 100, 0.20, 100), badge="flagged")).label == "suggestive"


def test_refuted_on_opposite_direction_or_no_difference():
    assert evaluate(cut(GD, COLS), res(rows(0.10, 100, 0.20, 100))).label == "refuted"
    assert evaluate(cut(GD, COLS), res(rows(0.201, 100, 0.20, 100))).label == "refuted"


@pytest.mark.parametrize("kw, why", [
    ({"status": "transparent_failure"}, "pipeline status"),
    ({"truncated": True}, "truncated"),
    ({"badge": "refused"}, "badge"),
])
def test_untestable_on_pipeline_problems(kw, why):
    r = res(rows(0.3, 100, 0.2, 100), **kw)
    v = evaluate(cut(GD, COLS), r)
    assert v.label == "untestable" and why in v.reason


def test_untestable_when_expected_columns_missing_or_arm_absent():
    v = evaluate(cut(GD, COLS + ["extra"]), res(rows(0.3, 100, 0.2, 100)))
    assert v.label == "untestable" and "missing expected columns" in v.reason
    t = TestSpec("group_diff", "rate", n_col="n", group_col="seg", a="x", b="zzz")
    assert evaluate(cut(t, COLS), res(rows(0.3, 100, 0.2, 100))).label == "untestable"


def test_columns_resolve_case_insensitively_and_bools_compare_as_strings():
    t = TestSpec("group_diff", "Rate", n_col="N", group_col="flag", a="true", b=False)
    r = [{"flag": True, "n": 50, "rate": 0.4}, {"flag": False, "n": 50, "rate": 0.2}]
    assert evaluate(cut(t, ["flag", "N", "Rate"]), res(r)).label == "confirmed"


def test_rest_pools_every_other_group_weighted_by_n():
    t = TestSpec("group_diff", "rate", n_col="n", group_col="seg", a="x", b="__rest__")
    r = [{"seg": "x", "n": 100, "rate": 0.4}, {"seg": "y", "n": 100, "rate": 0.1},
         {"seg": "z", "n": 300, "rate": 0.3}]
    v = evaluate(cut(t, COLS), res(r))
    assert v.stats["b"] == pytest.approx(0.25) and v.stats["n_b"] == 400


def test_before_after_splits_on_iso_dates_and_month_labels():
    t = TestSpec("before_after", "rate", n_col="n", time_col="month", split_at="2026-01-01", direction="higher")
    r = [{"month": "2025-11", "n": 100, "rate": 0.1}, {"month": "2025-12", "n": 100, "rate": 0.1},
         {"month": "2026-01", "n": 100, "rate": 0.2}, {"month": "2026-02", "n": 100, "rate": 0.2}]
    v = evaluate(cut(t, ["month", "n", "rate"]), res(r))
    assert v.label == "confirmed" and v.stats["n_a"] == 200
    assert iso_period("2026-Q2") == "2026-04-01" and iso_period("2026-03") == "2026-03-01"


def test_threshold_and_reconcile():
    t = TestSpec("threshold", "share", n_col="n", b=0.5, direction="higher")
    assert evaluate(cut(t, ["share", "n"]), res([{"share": 0.7, "n": 80}])).label == "confirmed"
    assert evaluate(cut(t, ["share", "n"]), res([{"share": 0.4, "n": 80}])).label == "refuted"
    rc = TestSpec("reconcile", "part", part_col="part", total_col="total")
    ok = [{"part": 50.0, "total": 100.0}, {"part": 50.0, "total": 100.0}]
    bad = [{"part": 60.0, "total": 100.0}, {"part": 50.0, "total": 100.0}]
    assert evaluate(cut(rc, ["part", "total"]), res(ok)).label == "confirmed"
    assert evaluate(cut(rc, ["part", "total"]), res(bad)).label == "refuted"


def test_no_denominator_never_confirms():
    t = TestSpec("group_diff", "rate", group_col="seg", a="x", b="y")
    v = evaluate(cut(t, ["seg", "rate"]), res([{"seg": "x", "rate": 0.4}, {"seg": "y", "rate": 0.1}]))
    assert v.label == "suggestive" and "no denominator" in v.reason


def test_thresholds_come_from_the_config():
    strict = JudgeConfig(min_rel_diff=0.6)
    assert evaluate(cut(GD, COLS), res(rows(0.30, 100, 0.20, 100)), strict).label == "suggestive"


def test_combine():
    assert combine(["confirmed", "untestable"]) == "confirmed"
    assert combine(["confirmed", "refuted"]) == "suggestive"
    assert combine(["refuted", "suggestive"]) == "refuted"
    assert combine(["suggestive"]) == "suggestive"
    assert combine(["untestable", "untestable"]) == combine([]) == "untestable"


def test_z_is_information_only():
    assert two_prop_z(0.3, 100, 0.2, 100) == pytest.approx(1.633, abs=1e-3)
    assert two_prop_z(3.0, 100, 0.2, 100) is None
