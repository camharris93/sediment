"""The planted-truth dataset is deterministic, committed exactly, and its truths
are filled in. Generating takes a few seconds (pure Python)."""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("saasco_generate", ROOT / "datasets" / "saasco" / "generate.py")
gen = importlib.util.module_from_spec(spec)
sys.modules["saasco_generate"] = gen   # dataclasses resolve annotations via sys.modules
spec.loader.exec_module(gen)


@pytest.fixture(scope="module")
def dev_tables():
    return gen.generate("dev")[0]


def test_same_seed_same_bytes(dev_tables):
    again = gen.generate("dev")[0]
    for t in gen.TABLES:
        assert hashlib.sha256(dev_tables[t]).digest() == hashlib.sha256(again[t]).digest(), t


@pytest.mark.parametrize("variant", ["dev", "holdout"])
def test_committed_csvs_match_the_generator(variant, dev_tables):
    tables = dev_tables if variant == "dev" else gen.generate("holdout")[0]
    d = ROOT / "datasets" / gen.VARIANTS[variant]["dataset"] / "data"
    for t in gen.TABLES:
        assert (d / f"{t}.csv").read_bytes() == tables[t], f"{variant}/{t}.csv is stale: rerun generate.py"


def test_every_table_has_a_single_surrogate_key(dev_tables):
    keys = {"accounts": "account_id", "subscriptions": "subscription_id", "invoices": "invoice_id",
            "usage_weekly": "usage_id", "support_tickets": "ticket_id"}
    for t, k in keys.items():
        lines = dev_tables[t].decode().splitlines()
        assert lines[0].split(",")[0] == k
        ids = [ln.split(",")[0] for ln in lines[1:]]
        assert len(ids) == len(set(ids))


def test_no_column_gives_a_story_away(dev_tables):
    banned = ("churn", "involuntary", "promo", "price_version", "grandfather", "renewal", "cohort", "activation",
              "decoy", "story", "planted")
    for t, data in dev_tables.items():
        header = data.decode().splitlines()[0].lower()
        assert not [b for b in banned if b in header], (t, header)


@pytest.mark.parametrize("key", ["saasco_answer_key.yaml", "saasco_holdout_answer_key.yaml"])
def test_truths_are_filled_in(key):
    k = yaml.safe_load((ROOT / "evals" / "investigations" / key).read_text())
    truths = [t for s in k["stories"] for t in s.get("truths") or []]
    assert len(truths) >= 15 and all(t["observed"] is not None for t in truths)


def test_staging_types_match_the_built_warehouse():
    import duckdb

    from engine.config import WAREHOUSE_PATH
    if not WAREHOUSE_PATH.exists():
        pytest.skip("no warehouse")
    con = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        rows = con.execute("select table_name, column_name, data_type from information_schema.columns "
                           "where table_schema = 'saasco_staging'").fetchall()
    finally:
        con.close()
    if not rows:
        pytest.skip("saasco not built")
    got = {(t, c): ty for t, c, ty in rows}
    for t, cols in gen.STAGING_TYPES.items():
        for c, ty in cols:
            assert got[(f"stg_{t}", c)] == ty, (t, c)
