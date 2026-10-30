"""Findings memory is append-only, and an investigation only ever PROPOSES."""
from __future__ import annotations

import pytest
import yaml

from engine.investigate import memory as mem
from engine.investigate.types import Claim, Finding, Investigation, SkepticCheck


def inv(run_id="r1", outcome="survived", label="confirmed") -> Investigation:
    f = Finding("F1", "h1", label, "Annual accounts churn at renewal.",
                Claim("logo_churn_rate", ["billing_interval"], {"billing_interval": "annual"}, "higher"),
                [], ["select 1"], [SkepticCheck("small_n", "h1.c1", [], "passed", "ok")], outcome)
    g = Finding("F2", "h2", "untestable", "Something untestable.", Claim("other", ["x"], {}, "higher"), [], [],
                note="h2.c1: missing columns")
    return Investigation(run_id, "why?", "demo", "full", findings=[f, g])


@pytest.fixture
def mpath(tmp_path, monkeypatch):
    p = tmp_path / "demo" / "findings.yml"
    monkeypatch.setenv("SEDIMENT_FINDINGS_PATH", str(p))
    return p


def test_missing_file_is_empty_memory(mpath):
    m = mem.load_findings("demo")
    assert m.is_empty() and m.sha is None and m.prompt_block() == ""


def test_propose_never_touches_findings_yml(mpath):
    p = mem.propose_delta(inv())
    assert p.parent.name == "findings.proposed" and p.exists()
    assert not mpath.exists()
    delta = yaml.safe_load(p.read_text())
    assert delta["findings"][0]["skeptic_outcome"] == "survived"
    assert delta["open_questions"][0]["text"].startswith("Untested:")


def test_accept_appends_with_sequential_ids_and_records_the_run(mpath):
    mem.propose_delta(inv("r1"))
    assert mem.accept("demo", "r1", by="cam") == ["f.0001", "o.0001"]
    mem.propose_delta(inv("r2"))
    assert mem.accept("demo", "r2", ids=["p.f1"], by="cam") == ["f.0002"]
    doc = mem.load_findings("demo").doc
    assert [f["id"] for f in doc["findings"]] == ["f.0001", "f.0002"]
    assert doc["findings"][0]["added_by"] == "cam"
    assert [r["run_id"] for r in doc["runs"]] == ["r1", "r2"]


def test_existing_entries_are_byte_for_byte_preserved(mpath):
    mem.propose_delta(inv("r1"))
    mem.accept("demo", "r1", by="cam")
    before = mem.load_findings("demo").doc
    mem.propose_delta(inv("r2"))
    mem.accept("demo", "r2", by="cam")
    after = mem.load_findings("demo").doc
    for s in mem.SECTIONS:
        assert after[s][:len(before[s])] == before[s]


def test_append_only_violation_is_detected():
    old = {"findings": [{"id": "f.0001", "statement": "a"}]}
    with pytest.raises(mem.AppendOnlyViolation):
        mem.assert_append_only(old, {"findings": [{"id": "f.0001", "statement": "EDITED"}]})
    with pytest.raises(mem.AppendOnlyViolation):
        mem.assert_append_only(old, {"findings": []})


def test_supersedes_hides_the_old_entry_from_prompts(mpath):
    mpath.parent.mkdir(parents=True)
    mpath.write_text(yaml.safe_dump({"version": 1, "dataset": "demo", "definitions": [
        {"id": "def.0001", "text": "old definition", "status": "accepted"},
        {"id": "def.0002", "text": "new definition", "status": "accepted", "supersedes": "def.0001"},
        {"id": "def.0003", "text": "unreviewed", "status": "proposed"}],
        "findings": [], "data_quirks": [], "open_questions": [], "runs": []}))
    m = mem.load_findings("demo")
    assert [d["id"] for d in m.definitions] == ["def.0002"]
    assert "old definition" not in m.prompt_block() and "unreviewed" not in m.prompt_block()


def test_eval_auto_accept_only_takes_survivors_and_quirks(mpath):
    mem.propose_delta(inv("r1", outcome="downgraded", label="suggestive"))
    assert mem.accept("demo", "r1", auto=True) == []
    mem.propose_delta(inv("r2", outcome="survived"))
    added = mem.accept("demo", "r2", auto=True)
    assert added == ["f.0001"]
    assert mem.load_findings("demo").doc["findings"][0]["added_by"] == "eval-auto"
