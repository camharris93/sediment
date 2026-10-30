"""The whole investigation loop, offline: a fake `engine._llm.complete` plays the
planner, L1, L2 and the skeptic; everything else (L3-L6, judge, skeptic checks,
report, memory proposal, budget) is the real code over a tiny DuckDB warehouse
with a planted Simpson's paradox."""
from __future__ import annotations

import json

import duckdb
import pytest

from engine._llm import LLMUsage
from engine.investigate.budget import Budget
from engine.investigate.memory import Memory
from engine.investigate.report import untraced_numbers
from engine.investigate.runner import investigate, load_investigation, replay
from engine.query.grounding import build_grounding_context
from engine.query.warehouse import DuckDBWarehouse

POOLED = ("SELECT m.sa, COUNT(m.account_month_id) AS n, AVG(CASE WHEN m.churned THEN 1.0 ELSE 0.0 END) AS rate "
          "FROM demo_marts.mart_months m GROUP BY m.sa")
STRAT = ("SELECT m.sa, m.band, COUNT(m.account_month_id) AS n, AVG(CASE WHEN m.churned THEN 1.0 ELSE 0.0 END) AS rate "
         "FROM demo_marts.mart_months m GROUP BY m.sa, m.band")
SHARE = ("SELECT m.band, COUNT(m.account_month_id) AS n, AVG(CASE WHEN m.sa THEN 1.0 ELSE 0.0 END) AS sa_share "
         "FROM demo_marts.mart_months m GROUP BY m.band")

PLAN = {"hypotheses": [
    {"statement": "Assisted accounts churn more.", "mechanism": "worse fit", "boring": False,
     "claim": {"metric": "logo_churn_rate", "dimensions": ["sa"], "segment": {"sa": True}, "direction": "higher"},
     "cuts": [{"question": "churn rate by sa", "expected_columns": ["sa", "n", "rate"], "grain": "sa",
               "test": {"kind": "group_diff", "value_col": "rate", "n_col": "n", "group_col": "sa",
                        "a": True, "b": False, "direction": "higher"}}]},
    {"statement": "Assistance is concentrated in the small band.", "mechanism": "mix", "boring": True,
     "claim": {"metric": "other", "dimensions": ["band", "sa"], "segment": {"band": "small"}, "direction": "higher"},
     "cuts": [{"question": "sa_share by band", "expected_columns": ["band", "n", "sa_share"], "grain": "band",
               "test": {"kind": "group_diff", "value_col": "sa_share", "n_col": "n", "group_col": "band",
                        "a": "small", "b": "big", "direction": "higher"}}]}]}


@pytest.fixture
def demo(tmp_path, monkeypatch):
    p = tmp_path / "demo.duckdb"
    con = duckdb.connect(str(p))
    con.execute("CREATE SCHEMA demo_marts")
    rows = []
    # small band: sa 900 @ 4%, not 100 @ 6%; big band: sa 100 @ 1%, not 900 @ 2%
    for band, sa, n, pct in [("small", True, 900, 4), ("small", False, 100, 6), ("big", True, 100, 1), ("big", False, 900, 2)]:
        for i in range(n):
            rows.append((f"{band}-{sa}-{i}", band, sa, (i % 100) < pct))
    con.execute("CREATE TABLE demo_marts.mart_months (account_month_id VARCHAR, band VARCHAR, sa BOOLEAN, churned BOOLEAN)")
    con.executemany("INSERT INTO demo_marts.mart_months VALUES (?, ?, ?, ?)", rows)
    con.close()
    wh = DuckDBWarehouse(path=p)
    import engine.query.dry_run as dr
    import engine.query.execution as ex
    monkeypatch.setattr(ex, "DuckDBWarehouse", lambda *a, **k: wh)
    monkeypatch.setattr(dr, "DuckDBWarehouse", lambda *a, **k: wh)
    monkeypatch.setenv("SEDIMENT_FINDINGS_PATH", str(tmp_path / "mem" / "findings.yml"))
    return build_grounding_context("demo", warehouse=wh)


def fake(monkeypatch, plans=None, checks=None):
    from engine import _llm
    calls: list[str] = []
    plans = list(plans or [json.dumps(PLAN)])

    def complete(*, system, user, model=None, max_tokens=4096, cache_system=True):
        layer = system.split(" layer")[0].split("the ")[-1].strip()
        calls.append(layer)
        if layer == "PLANNING":
            text = plans.pop(0) if len(plans) > 1 else plans[0]
        elif layer == "SKEPTIC":
            text = json.dumps(checks or {"checks": [{"kind": "simpsons", "target_cut_id": "h1.c1", "confounder": "band"}]})
        elif layer == "INTENT":
            q = user.removeprefix("Question: ").splitlines()[0]
            text = json.dumps({"restated_question": q, "target_tables": ["demo_marts.mart_months"],
                               "expected_result_grain": "g", "metrics": [], "filters": [], "date_range": None,
                               "assumptions": ["churn means churned = true"]})
        elif layer == "GENERATION":
            text = STRAT if "break the result down by band" in user else SHARE if "sa_share" in user else POOLED
        else:
            raise AssertionError(f"unexpected LLM layer {layer}")
        u = LLMUsage(model=model or "claude-sonnet-4-6", input_tokens=100, output_tokens=20)
        _llm.record_usage(u)
        return text, u
    monkeypatch.setattr("engine._llm.complete", complete)
    return calls


def test_full_loop_judge_confirms_and_skeptic_refutes_simpsons(demo, monkeypatch, tmp_path):
    calls = fake(monkeypatch)
    inv = investigate("does assistance hurt retention?", "demo", ctx=demo, out_dir=tmp_path / "runs",
                      memory=Memory("demo"))
    f1, f2 = inv.findings
    assert (f1.judged_label, f1.label, f1.skeptic_outcome) == ("confirmed", "refuted", "refuted")
    assert f1.skeptic[0].kind == "simpsons" and f1.skeptic[0].outcome == "failed"
    assert f2.label == "confirmed"
    # 1 plan + 2 cuts x (L1 + L2) + a skeptic chooser per confirmed finding (2) + 1 skeptic
    # cut x (L1 + L2); no L7. F2's proposed confounder is one of its own dimensions, so it
    # is dropped and F2 gets no checks.
    assert calls.count("PLANNING") == 1 and calls.count("SKEPTIC") == 2
    assert calls.count("INTENT") == calls.count("GENERATION") == 3 and "TRANSLATION" not in calls
    assert inv.usage["calls"] == 9 and inv.usage["queries"] == 3 and inv.usage["usd"] > 0
    assert f2.skeptic == [] and f2.skeptic_outcome == "not_run"
    d = tmp_path / "runs" / inv.run_id
    md = (d / "report.md").read_text()
    assert untraced_numbers(md, inv) == []
    assert (d / "trace.jsonl").read_text().count('"cut_id": "h1.s1"') > 0
    assert load_investigation(d).findings[0].label == "refuted"


def test_replay_of_a_recorded_run_reproduces_it_without_an_llm(demo, monkeypatch, tmp_path):
    fake(monkeypatch)
    inv = investigate("q", "demo", ctx=demo, out_dir=tmp_path / "runs", memory=Memory("demo"))

    def boom(**_k):
        raise AssertionError("replay must not call the LLM")
    monkeypatch.setattr("engine._llm.complete", boom)
    again = replay(load_investigation(tmp_path / "runs" / inv.run_id), demo)
    assert [(f.judged_label, f.label) for f in again.findings] == [(f.judged_label, f.label) for f in inv.findings]
    assert again.mode == "replay" and again.usage["calls"] == 0


def test_no_skeptic_config_ships_the_false_finding(demo, monkeypatch, tmp_path):
    fake(monkeypatch)
    inv = investigate("q", "demo", config="no_skeptic", ctx=demo, out_dir=None, memory=Memory("demo"))
    assert inv.findings[0].label == "confirmed" and inv.findings[0].skeptic_outcome == "not_run"


def test_budget_stop_marks_remaining_cuts_untestable(demo, monkeypatch):
    fake(monkeypatch)
    inv = investigate("q", "demo", ctx=demo, out_dir=None, memory=Memory("demo"), budget=Budget(max_queries=1))
    assert inv.stopped_reason == "budget_queries"
    assert inv.findings[1].label == "untestable" and "budget_queries" in inv.findings[1].note


def test_planner_gets_one_repair_then_fails_transparently(demo, monkeypatch):
    bad = json.dumps({"hypotheses": [{"statement": "x", "claim": {"metric": "vibes"}, "cuts": []}]})
    calls = fake(monkeypatch, plans=[bad, json.dumps(PLAN)])
    inv = investigate("q", "demo", ctx=demo, out_dir=None, memory=Memory("demo"))
    assert calls.count("PLANNING") == 2 and inv.error is None and len(inv.findings) == 2
    calls = fake(monkeypatch, plans=[bad])
    inv = investigate("q", "demo", ctx=demo, out_dir=None, memory=Memory("demo"))
    assert calls.count("PLANNING") == 2 and inv.error and "planner failed" in inv.error and inv.findings == []


def test_proposal_is_written_but_findings_yml_is_not(demo, monkeypatch, tmp_path):
    fake(monkeypatch)
    inv = investigate("q", "demo", ctx=demo, out_dir=tmp_path / "runs")
    mem_dir = tmp_path / "mem"
    assert (mem_dir / "findings.proposed" / f"{inv.run_id}.yml").exists()
    assert not (mem_dir / "findings.yml").exists()


def test_b2_baseline_runs_model_sql_through_l3_l5_and_keeps_its_labels(demo, monkeypatch, tmp_path):
    from engine import _llm
    from engine.investigate.baselines import run_b2

    payload = {"findings": [
        {"statement": "assisted churn more", "label": "confirmed", "sql": POOLED,
         "claim": {"metric": "logo_churn_rate", "dimensions": ["sa"], "segment": {"sa": True}, "direction": "higher"}},
        {"statement": "bad sql", "label": "confirmed", "sql": "SELECT nope FROM demo_marts.mart_months",
         "claim": {"metric": "other", "dimensions": ["sa"], "segment": {}, "direction": "higher"}}]}

    def complete(*, system, user, model=None, max_tokens=4096, cache_system=True):
        assert "senior data analyst" in system and "demo_marts.mart_months" in system
        u = LLMUsage(model=model or "m", input_tokens=50, output_tokens=50)
        _llm.record_usage(u)
        return json.dumps(payload), u
    monkeypatch.setattr("engine._llm.complete", complete)
    inv = run_b2("q", "demo", demo, out_dir=tmp_path)
    assert [f.label for f in inv.findings] == ["confirmed", "untestable"]
    assert inv.findings[0].evidence and inv.usage["calls"] == 1
    assert (tmp_path / inv.run_id / "report.md").exists()
