"""The planner, skeptic and B2 prompts — and the judge's defaults — must not
know the answers. Generic statistics vocabulary ("mix shift", "small sample") is
allowed: it is the skeptic's menu."""
from __future__ import annotations

from dataclasses import astuple
from pathlib import Path

import pytest
import yaml

from engine.investigate.baselines import B2_SYSTEM
from engine.investigate.evaluate import DEFAULT_JUDGE
from engine.investigate.plan import PLANNER_SYSTEM
from engine.investigate.skeptic import _DEDUP, SKEPTIC_SYSTEM
from engine.investigate.types import SKEPTIC_KINDS

ROOT = Path(__file__).resolve().parents[1]
KEYWORDS = ["prepay", "promo", "renewal", "grandfather", "price change", "price increase", "failed payment",
            "payment failure", "involuntary", "activation", "simpson", "paid_social", "paid social", "healthcare",
            "europe", "latam", "apac", "starter", "annual", "billing_interval", "sales_assisted",
            "sales assist", "saasco", "churn"]


def _key_terms() -> set[str]:
    """Segment values named in the answer keys' match signatures."""
    terms = set()
    for k in ("saasco_answer_key.yaml", "saasco_holdout_answer_key.yaml"):
        for s in yaml.safe_load((ROOT / "evals" / "investigations" / k).read_text())["stories"]:
            for sig in (s.get("match") or []) + (s.get("match_correct") or []) + (s.get("match_naive") or []):
                for v in (sig.get("segment") or {}).values():
                    if isinstance(v, str) and len(v) > 2:
                        terms.add(v.lower())
    return terms


PROMPTS = {"planner": PLANNER_SYSTEM, "skeptic": SKEPTIC_SYSTEM, "b2": B2_SYSTEM, "dedup": _DEDUP}
# The skeptic's own menu names are generic statistics, allowed in ITS prompt only.
MENU_OK = {"skeptic": set(SKEPTIC_KINDS)}


@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_prompt_has_no_answer_key_vocabulary(name):
    text = PROMPTS[name].lower()
    for allowed in MENU_OK.get(name, ()):
        text = text.replace(allowed, "")
    hits = [k for k in KEYWORDS + sorted(_key_terms()) if k in text]
    assert not hits, f"{name} prompt leaks: {hits}"


def test_region_codes_are_not_in_prompts():
    import re
    for name, text in PROMPTS.items():
        assert not re.search(r"\b(EU|NA|APAC|LATAM)\b", text), name


def test_judge_config_is_generic_numbers_only():
    assert all(isinstance(v, (int, float, tuple)) for v in astuple(DEFAULT_JUDGE))
