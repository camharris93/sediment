"""Skeptic check functions on planted rows. Each check must catch its artifact
and pass the honest version."""
from __future__ import annotations

from engine.investigate.skeptic import (
    build_skeptic_cut,
    check_fanout,
    check_mix_shift,
    check_simpsons,
    check_small_n,
    outcome_of,
    run_check,
    validate_checks,
)
from engine.investigate.types import Claim, Cut, CutResult, Finding, Hypothesis, SkepticCheck, TestSpec

CFG = __import__("engine.investigate.evaluate", fromlist=["DEFAULT_JUDGE"]).DEFAULT_JUDGE
GD = TestSpec("group_diff", "rate", n_col="n", group_col="sa", a=True, b=False, direction="higher")
TARGET = Cut("h1.c1", "h1", "test", "churn by sa", ["sa", "n", "rate"], "one row per sa", GD)


def r(rows, cid="h1.s1", warnings=None):
    return CutResult(cid, "executed", "select 1", rows, "clean", warnings or [])


def simpson_rows(within_lower: bool):
    # sa concentrated in the high-churn small band; within each band sa is lower (or higher)
    lo, hi = (0.04, 0.06) if within_lower else (0.07, 0.05)
    return [{"sa": True, "band": "small", "n": 900, "rate": lo}, {"sa": False, "band": "small", "n": 100, "rate": hi},
            {"sa": True, "band": "big", "n": 100, "rate": lo / 3}, {"sa": False, "band": "big", "n": 900, "rate": hi / 3}]


def test_simpsons_fails_when_direction_reverses_within_strata():
    out, why, cells = check_simpsons(TARGET, r(simpson_rows(within_lower=True)), "band", CFG)
    assert out == "failed" and "REVERSES" in why and cells


def test_simpsons_passes_when_direction_holds():
    assert check_simpsons(TARGET, r(simpson_rows(within_lower=False)), "band", CFG)[0] == "passed"


def test_simpsons_untestable_with_one_stratum():
    rows = [x for x in simpson_rows(True) if x["band"] == "small"]
    assert check_simpsons(TARGET, r(rows), "band", CFG)[0] == "untestable"


BA = TestSpec("before_after", "rate", n_col="n", time_col="period", split_at="2026-01-01", direction="higher")
BA_CUT = Cut("h2.c1", "h2", "test", "q", ["period", "n", "rate"], "g", BA)


def mix_rows(rates_move: bool):
    # channel mix shifts toward the high-churn channel; rates constant unless rates_move
    bump = 0.10 if rates_move else 0.0
    return [{"period": "2025-06", "ch": "hi", "n": 100, "rate": 0.20}, {"period": "2025-06", "ch": "lo", "n": 900, "rate": 0.05},
            {"period": "2026-03", "ch": "hi", "n": 500, "rate": 0.20 + bump}, {"period": "2026-03", "ch": "lo", "n": 500, "rate": 0.05 + bump}]


def test_mix_shift_fails_when_the_change_is_pure_composition():
    out, why, _ = check_mix_shift(BA_CUT, r(mix_rows(False)), "ch", CFG)
    assert out == "failed" and "mix shift" in why


def test_mix_shift_passes_when_rates_really_moved():
    assert check_mix_shift(BA_CUT, r(mix_rows(True)), "ch", CFG)[0] == "passed"


def test_small_n_fails_on_too_few_events_even_when_n_clears_min_n():
    rows = [{"sa": True, "n": 32, "rate": 0.28}, {"sa": False, "n": 1800, "rate": 0.12}]
    out, why, _ = check_small_n(TARGET, r(rows, "h1.c1"), CFG)
    assert out == "failed" and "too few events" in why
    rows = [{"sa": True, "n": 400, "rate": 0.28}, {"sa": False, "n": 1800, "rate": 0.12}]
    assert check_small_n(TARGET, r(rows, "h1.c1"), CFG)[0] == "passed"


def test_fanout_fails_on_l6_warning_or_reconcile_gap():
    orig = r([{"sa": True, "n": 100, "rate": 0.3}, {"sa": False, "n": 100, "rate": 0.2}], "h1.c1",
             warnings=[{"kind": "fanout_risk", "message": "COUNT(*) over a join"}])
    assert check_fanout(TARGET, orig, None, CFG)[0] == "failed"
    orig.warnings = []
    dedup = r([{"sa": True, "n": 100, "rate": 0.25}, {"sa": False, "n": 100, "rate": 0.2}])
    assert check_fanout(TARGET, orig, dedup, CFG)[0] == "failed"
    same = r([{"sa": True, "n": 100, "rate": 0.3}, {"sa": False, "n": 100, "rate": 0.2}])
    assert check_fanout(TARGET, orig, same, CFG)[0] == "passed"


def test_skeptic_only_lowers_labels():
    mk = lambda kind, out: SkepticCheck(kind, "h1.c1", [], out, "")  # noqa: E731
    assert outcome_of([mk("simpsons", "failed")]) == ("refuted", "refuted")
    assert outcome_of([mk("fanout_or_bug", "failed")]) == ("refuted", "refuted")
    assert outcome_of([mk("mix_shift", "failed"), mk("small_n", "passed")]) == ("downgraded", "suggestive")
    assert outcome_of([mk("small_n", "passed")]) == ("survived", None)
    assert outcome_of([mk("small_n", "untestable")]) == ("not_run", None)
    assert outcome_of([]) == ("not_run", None)


def test_skeptic_cuts_are_built_deterministically_from_the_target():
    s = build_skeptic_cut({"kind": "simpsons", "confounder": "band"}, TARGET, "h1.s1")
    assert s.purpose == "skeptic" and "band" in s.expected_columns and s.test == TARGET.test
    assert build_skeptic_cut({"kind": "small_n"}, TARGET, "h1.s1") is None


def test_validate_checks_drops_bad_confounders(grounding):
    f = Finding("F1", "h1", "confirmed", "s", Claim("logo_churn_rate", ["class"], {}, "higher"), [], [])
    h = Hypothesis("h1", "s", "m", f.claim, [TARGET])
    payload = {"checks": [{"kind": "simpsons", "confounder": "not_a_column"},
                          {"kind": "simpsons", "confounder": "class"},          # already a dimension
                          {"kind": "mix_shift", "confounder": "species", "target_cut_id": "h1.c1"},
                          {"kind": "small_n"}]}
    out = validate_checks(payload, f, h, grounding)
    assert [c["kind"] for c in out] == ["mix_shift", "small_n"]


def test_run_check_end_to_end_on_rows():
    results = {"h1.c1": r([{"sa": True, "n": 1000, "rate": 0.0385}, {"sa": False, "n": 1000, "rate": 0.0316}], "h1.c1")}
    scut = build_skeptic_cut({"kind": "simpsons", "confounder": "band"}, TARGET, "h1.s1")
    results["h1.s1"] = r(simpson_rows(True))
    sc = run_check({"kind": "simpsons", "confounder": "band"}, TARGET, results, scut)
    assert sc.outcome == "failed" and sc.cut_ids == ["h1.s1"]
