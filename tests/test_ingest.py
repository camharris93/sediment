"""Generic ingestion — any tabular file lands in <schema>.<table>, auto-typed,
columns preserved verbatim (rename/cast is staging's job, not ingest's)."""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from engine.ingest import ingest_file, raw_schema


def _csv(tmp_path: Path) -> Path:
    p = tmp_path / "src.csv"
    p.write_text("id,name,weight\n1,Olm,17.0\n2,Mouse,20.0\n", encoding="utf-8")
    return p


def test_ingest_lands_rows_and_columns(tmp_path):
    wh = tmp_path / "w.duckdb"
    n = ingest_file(_csv(tmp_path), "critters", schema="raw", warehouse=wh)
    assert n == 2
    con = duckdb.connect(str(wh), read_only=True)
    try:
        cols = [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='raw' AND table_name='critters' ORDER BY ordinal_position"
        ).fetchall()]
        assert cols == ["id", "name", "weight"]
        # auto-typing: id integer-ish, weight floating
        types = {r[0]: r[1] for r in con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='raw' AND table_name='critters'").fetchall()}
        assert "INT" in types["id"].upper() or "BIGINT" in types["id"].upper()
        assert types["weight"].upper() in ("DOUBLE", "FLOAT", "DECIMAL")
    finally:
        con.close()


def test_ingest_is_idempotent(tmp_path):
    wh = tmp_path / "w.duckdb"
    src = _csv(tmp_path)
    ingest_file(src, "t", schema="raw", warehouse=wh)
    n2 = ingest_file(src, "t", schema="raw", warehouse=wh)  # CREATE OR REPLACE
    assert n2 == 2
    con = duckdb.connect(str(wh), read_only=True)
    try:
        (count,) = con.execute("SELECT COUNT(*) FROM raw.t").fetchone()
        assert count == 2  # not doubled
    finally:
        con.close()


def test_ingest_respects_delimiter(tmp_path):
    p = tmp_path / "tab.txt"
    p.write_text("a\tb\n1\tx\n2\ty\n", encoding="utf-8")
    wh = tmp_path / "w.duckdb"
    n = ingest_file(p, "tabbed", schema="raw", delimiter="\t", warehouse=wh)
    assert n == 2
    con = duckdb.connect(str(wh), read_only=True)
    try:
        cols = [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='tabbed' ORDER BY ordinal_position").fetchall()]
        assert cols == ["a", "b"]
    finally:
        con.close()


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest_file(tmp_path / "nope.csv", "t", warehouse=tmp_path / "w.duckdb")


def test_raw_schema_namespacing():
    assert raw_schema("anage") == "anage_raw"
    assert raw_schema("nba") == "nba_raw"
