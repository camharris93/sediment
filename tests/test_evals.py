"""Bridge the eval harness into pytest.

The replay evals need the REAL anage warehouse (curated values like the rockfish at
rank 1), so they're skipped when warehouse.duckdb is absent — CI runs them in the
build job via `python evals/harness.py` after `run.py up`. When you've built locally
they run here too. The live tier is gated behind the `live` marker + a key.
"""
from __future__ import annotations

import os

import pytest

from engine.config import WAREHOUSE_PATH, has_anthropic_key
from engine.query.grounding import build_grounding_context
from evals.harness import load_cases, run_live, run_replay

_NO_WAREHOUSE = not WAREHOUSE_PATH.exists()


@pytest.mark.skipif(_NO_WAREHOUSE, reason="needs a built warehouse (run.py up)")
def test_replay_golden_evals_all_pass():
    grounding = build_grounding_context("anage")
    results = run_replay(grounding, load_cases())
    failures = [f"{r.id}: {r.detail}" for r in results if not r.ok]
    assert not failures, "replay eval failures:\n" + "\n".join(failures)
    assert len(results) >= 10  # guard against an empty/short run silently passing


@pytest.mark.live
@pytest.mark.skipif(_NO_WAREHOUSE, reason="needs a built warehouse (run.py up)")
@pytest.mark.skipif(not has_anthropic_key() or os.environ.get("CI"),
                    reason="live tier needs an Anthropic key and is skipped in CI")
def test_live_golden_evals_pass():
    grounding = build_grounding_context("anage")
    results = run_live(grounding, load_cases())
    passed = sum(1 for r in results if r.ok)
    # Live generation is probabilistic; require a high pass rate, not perfection.
    assert passed >= int(0.8 * len(results)), \
        "live eval pass rate below 80%:\n" + "\n".join(
            f"{r.id}: {r.detail}" for r in results if not r.ok)
