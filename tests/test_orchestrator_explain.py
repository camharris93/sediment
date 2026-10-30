"""explain=False makes zero L7 calls; the default path is unchanged."""
from __future__ import annotations

from engine._llm import LLMUsage, usage_meter

INTENT = '{"restated_question": "q", "target_tables": ["anage_marts.mart_longevity_by_class"], ' \
         '"expected_result_grain": "class", "metrics": [], "filters": [], "date_range": null, "assumptions": ["a"]}'
SQL = "SELECT class, n_species FROM anage_marts.mart_longevity_by_class ORDER BY n_species DESC"


def fake_llm(monkeypatch, calls):
    from engine import _llm

    def complete(*, system, user, model=None, max_tokens=4096, cache_system=True):
        layer = system.split("layer")[0].split("the ")[-1].strip()
        calls.append(layer)
        text = {"INTENT": INTENT, "GENERATION": SQL}.get(layer, "Explained.")
        u = LLMUsage(model=model or "fake", input_tokens=10, output_tokens=5)
        _llm.record_usage(u)
        return text, u
    monkeypatch.setattr("engine._llm.complete", complete)


def _wire(monkeypatch, warehouse):
    import engine.query.dry_run as dr
    import engine.query.execution as ex
    from engine.query.warehouse import DuckDBWarehouse
    monkeypatch.setattr(ex, "DuckDBWarehouse", lambda *a, **k: warehouse, raising=False)
    monkeypatch.setattr(dr, "DuckDBWarehouse", lambda *a, **k: warehouse, raising=False)
    return DuckDBWarehouse


def test_explain_false_skips_l7_and_default_calls_it(monkeypatch, grounding, warehouse):
    from engine.query.orchestrator import run_to_executed_answer
    _wire(monkeypatch, warehouse)
    calls: list[str] = []
    fake_llm(monkeypatch, calls)
    with usage_meter() as m:
        res = run_to_executed_answer("how many species per class?", grounding, explain=False)
    assert res.status == "executed", res
    assert calls == ["INTENT", "GENERATION"] and m.calls == 2
    assert res.final_answer.trust_badge.value == "clean"
    calls.clear()
    res2 = run_to_executed_answer("how many species per class?", grounding)
    assert calls == ["INTENT", "GENERATION", "TRANSLATION"]
    assert res2.final_answer.trust_badge == res.final_answer.trust_badge


def test_nested_meters_both_count():
    from engine import _llm
    with usage_meter() as outer:
        _llm.record_usage(LLMUsage("m", 1, 1))
        with usage_meter() as inner:
            _llm.record_usage(LLMUsage("m", 2, 2))
    assert (outer.calls, inner.calls) == (2, 1)
    assert outer.input_tokens == 3 and inner.output_tokens == 2
