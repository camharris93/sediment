"""Profiler — candidate-key detection and column stats feed the scaffolder, so the
key inference in particular must be right (it drives which not_null/unique tests
get proposed)."""
from __future__ import annotations

from pathlib import Path

from engine.ingest import ingest_file
from engine.profile import ColumnProfile, TableProfile, infer_relationships, profile_table


def _landed(tmp_path: Path):
    src = tmp_path / "s.csv"
    src.write_text(
        "id,species,class\n1,Olm,Amphibia\n2,Mouse,Mammalia\n3,Rat,Mammalia\n",
        encoding="utf-8")
    wh = tmp_path / "w.duckdb"
    ingest_file(src, "critters", schema="raw", warehouse=wh)
    return wh


def test_profile_basic_shape(tmp_path):
    wh = _landed(tmp_path)
    prof = profile_table("raw", "critters", dataset="x", warehouse=wh)
    assert prof.row_count == 3
    assert prof.column_count == 3
    names = [c.name for c in prof.columns]
    assert names == ["id", "species", "class"]


def test_candidate_key_detection(tmp_path):
    wh = _landed(tmp_path)
    prof = profile_table("raw", "critters", dataset="x", warehouse=wh)
    keys = {c.name for c in prof.columns if c.is_candidate_key}
    # id and species are unique per row; class repeats (Mammalia x2) so is NOT a key.
    assert "id" in keys
    assert "class" not in keys


def test_null_rate_computed(tmp_path):
    src = tmp_path / "n.csv"
    src.write_text("a,b\n1,x\n2,\n3,\n", encoding="utf-8")  # b null 2/3
    wh = tmp_path / "w.duckdb"
    ingest_file(src, "t", schema="raw", warehouse=wh)
    prof = profile_table("raw", "t", dataset="x", warehouse=wh)
    b = next(c for c in prof.columns if c.name == "b")
    assert b.null_rate > 0.6


def test_infer_relationships_one_to_many():
    # customers.customer_id (unique) -> orders.customer_id (repeats) = 1:many
    customers = TableProfile("d", "raw", "customers", 2, 1, [
        ColumnProfile("customer_id", "INTEGER", 0, 0.0, 2, is_candidate_key=True)], "")
    orders = TableProfile("d", "raw", "orders", 3, 1, [
        ColumnProfile("customer_id", "INTEGER", 0, 0.0, 2, is_candidate_key=False)], "")
    rels = infer_relationships([customers, orders])
    assert len(rels) == 1
    assert rels[0]["parent"] == "customers"
    assert rels[0]["child"] == "orders"
    assert rels[0]["cardinality"] == "1:many"
