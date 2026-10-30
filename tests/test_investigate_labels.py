"""Protocol deviation 2026-10-30: arm labels and quoted labels (docs/investigate.md)."""
from __future__ import annotations

from engine.investigate.cuts import pipeline_question
from engine.investigate.evaluate import evaluate, label_key, same_label
from engine.investigate.report import mask_numerals, untraced_numbers
from engine.investigate.types import REST, Cut, CutResult, Investigation, TestSpec


def _cut(a, b, kind="group_diff"):
    return Cut("h1.c1", "h1", "test", "churn by period", ["period", "rate", "n"], "period",
               TestSpec(kind=kind, value_col="rate", n_col="n", group_col="period", a=a, b=b))


def test_label_key_tolerates_punctuation_and_order():
    assert same_label("Q2-2026", "q2 2026")
    assert same_label("2026-Q2", "q2 2026")
    assert same_label("Q2_2026", "Q2-2026")
    assert label_key("2026-Q2") == ("2026", "q2")


def test_label_key_does_not_conflate_different_buckets():
    assert not same_label("1-6", "1-3 months")
    assert not same_label("7+", "7-12 months")
    assert not same_label("starter", "growth")


def test_judge_matches_arm_labels_across_formats():
    rows = [{"period": "q2 2026", "rate": 0.06, "n": 500},
            {"period": "q1 2026", "rate": 0.04, "n": 500},
            {"period": "2025", "rate": 0.04, "n": 500}]
    r = CutResult("h1.c1", "executed", "select 1", rows, "clean")
    v = evaluate(_cut("Q2-2026", REST), r)
    assert v.label == "confirmed", v.reason
    v = evaluate(_cut("2026-Q2", "2025"), r)
    assert v.label == "confirmed", v.reason
    v = evaluate(_cut("Q3-2026", REST), r)
    assert v.label == "untestable" and "no rows" in v.reason


def test_cut_question_states_arm_labels():
    q = pipeline_question(_cut("Q2-2026", REST), [])
    assert "'Q2-2026'" in q and "__rest__" not in q
    q = pipeline_question(_cut("1-6", "7+"), [])
    assert "'1-6'" in q and "'7+'" in q
    # before_after with no segment: nothing to state
    c = Cut("h1.c2", "h1", "test", "q", ["month", "rate", "n"], "month",
            TestSpec(kind="before_after", value_col="rate", n_col="n", time_col="month", split_at="2026-04-01"))
    assert pipeline_question(c, []).endswith("do not return raw rows.")


def test_mask_keeps_quoted_labels_only_when_asked():
    reason = "no rows for tenure_bucket='1-6' vs '7+' (values seen: ['1-3 months', '13+ months'])"
    assert mask_numerals(reason) == "no rows for tenure_bucket='#-#' vs '#+' (values seen: ['#-# months', '#+ months'])"
    assert mask_numerals(reason, keep_quoted=True) == reason
    # planner text never gets the exemption: a quoted number is still masked
    assert mask_numerals("churn rose '30%' in Q2") == "churn rose '#' in Q2"


def test_tracer_ignores_quoted_labels_in_reasons():
    md = ("<!-- findings:start -->\n#### F1 · x\n- **Claim:** `m` is **higher** for tenure_month = `3-6`\n"
          "- `h4.c2` → **untestable** (badge `clean`): no rows for tenure_bucket='1-6' vs '7+' "
          "(values seen: ['1-3 months', '13+ months'])\n- stray 42 here\n<!-- findings:end -->")
    inv = Investigation("r", "q", "d", "full", [], [], [], [], [])
    assert untraced_numbers(md, inv) == ["42"]
